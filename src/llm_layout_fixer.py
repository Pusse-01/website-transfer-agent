"""
LLM-assisted layout fixer.

Pipeline:
1. Capture a screenshot of the ORIGINAL live page.
2. Render the locally-processed HTML in a headless browser (same viewport
   that Builder.io uses for its Custom Code container) and capture a
   preview screenshot.
3. Send both screenshots + the current HTML to an OpenAI vision model and
   ask it to return a corrected HTML string whose rendered layout matches
   the original.
4. Validate the response and return the corrected HTML, or fall back to
   the input HTML if anything goes wrong.

This runs BEFORE uploading to Builder.io, so any layout drift caught by
the LLM is fixed in-place before the content ever reaches the CMS.

Environment variables:
- OPENAI_API_KEY     — required to enable this module.
- OPENAI_MODEL       — default "gpt-4o" (must support vision).
- LLM_LAYOUT_FIX     — set to "0" to explicitly disable even when a key exists.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


# Most capable OpenAI vision model available via API as of April 2025.
# Override via OPENAI_MODEL env var.  gpt-4.1 > gpt-4o for layout fidelity.
# gpt-4o-mini is the final fallback — it has the highest rate-limit ceiling
# of any OpenAI vision model and is a lifesaver when the premium models are
# throttled on your account tier.
DEFAULT_MODEL = "gpt-4.1"
FALLBACK_MODEL = "gpt-4o"
LAST_RESORT_MODEL = "gpt-4o-mini"
_RATE_LIMIT_RETRIES = 3        # max retries on 429 before giving up
_RATE_LIMIT_INITIAL_WAIT = 5   # seconds (doubles each retry)
_RATE_LIMIT_MAX_WAIT = 60      # cap — don't honor Retry-After values > this
# Keep the payload lean so we don't blow our per-minute token budget on a
# single call. Screenshots at detail:"low" cost ~85 tokens each (vs ~1500
# for "high"); 30K of HTML is plenty to fix layout issues without context.
_HTML_MAX_CHARS = 30_000
_IMAGE_DETAIL = "low"
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 800

# Raised by _post() when OpenAI returns a hard-stop 429 (billing quota,
# no credits). Retrying doesn't help — surface to the caller immediately.
class _QuotaExceeded(Exception):
    pass


def is_enabled() -> bool:
    """Return True when the LLM layout fixer is configured and not disabled."""
    if os.environ.get("LLM_LAYOUT_FIX", "").strip() == "0":
        return False
    return bool(os.environ.get("OPENAI_API_KEY"))


def _check_playwright() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


async def _screenshot_url(url: str, out_path: str) -> bool:
    """Capture a full-page screenshot of a live URL.

    Uses domcontentloaded (fast, reliable) instead of networkidle.
    Heavy pages with analytics/chat widgets can keep a connection open
    indefinitely, causing networkidle to time out. We navigate, wait for
    DOM, give JS a couple of extra seconds to paint, then screenshot.
    If the goto itself times out we still attempt a screenshot of
    whatever has loaded rather than giving up entirely.
    """
    if not _check_playwright():
        logger.warning("Playwright not installed — cannot capture original screenshot.")
        return False

    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={
                    "width": DEFAULT_VIEWPORT_WIDTH,
                    "height": DEFAULT_VIEWPORT_HEIGHT,
                },
                ignore_https_errors=True,
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                # The page took too long but may still be partially rendered.
                # Try to screenshot whatever is there rather than aborting.
                logger.warning(
                    "Timeout navigating to %s — taking screenshot of partial load.", url
                )
            await page.wait_for_timeout(2500)
            # Scroll to bottom to trigger lazy-load images, then back to top.
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
            except Exception:
                pass  # non-fatal; proceed to screenshot
            await page.screenshot(path=out_path, full_page=True)
            await browser.close()
        return True
    except Exception as e:
        logger.warning("Failed to screenshot %s: %s", url, e)
        return False


async def _screenshot_html(html_content: str, out_path: str) -> bool:
    """Render the processed HTML in a headless browser and screenshot it.

    The HTML is wrapped in a minimal host document that mirrors Builder.io's
    Custom Code container: a 1280px centred stage with the default system
    font. This is the same environment the content will live in after upload.
    """
    if not _check_playwright():
        return False

    from playwright.async_api import async_playwright

    host_doc = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>body{margin:0;background:#fff;}"
        ".builder-stage{max-width:1280px;margin:0 auto;padding:0;}"
        "</style></head><body><div class=\"builder-stage\">"
        f"{html_content}"
        "</div></body></html>"
    )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", delete=False, encoding="utf-8"
    ) as f:
        f.write(host_doc)
        html_path = f.name

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={
                    "width": DEFAULT_VIEWPORT_WIDTH,
                    "height": DEFAULT_VIEWPORT_HEIGHT,
                },
            )
            page = await context.new_page()
            try:
                await page.goto(
                    f"file://{html_path}", wait_until="domcontentloaded", timeout=30000
                )
            except Exception:
                pass  # partial load is fine — screenshot what's there
            await page.wait_for_timeout(1200)
            await page.screenshot(path=out_path, full_page=True)
            await browser.close()
        return True
    except Exception as e:
        logger.warning("Failed to render preview HTML: %s", e)
        return False
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass


def _b64_image(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        logger.warning("Could not read screenshot %s: %s", path, e)
        return None


def _extract_html_from_response(text: str) -> str | None:
    """Pull an HTML fragment out of an LLM reply.

    Accepts either a fenced ```html ...``` block or a raw HTML string that
    begins with a '<' tag.
    """
    if not text:
        return None

    fenced = re.search(r"```(?:html)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
        if candidate.startswith("<"):
            return candidate

    stripped = text.strip()
    if stripped.startswith("<"):
        return stripped

    return None


def _parse_openai_error(response) -> tuple[str, str]:
    """Extract (error_code, human_message) from an OpenAI error response."""
    try:
        body = response.json()
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = (err.get("code") or err.get("type") or "").strip()
        msg = (err.get("message") or "").strip()
        return code, msg
    except Exception:
        return "", ""


def _call_openai_vision(
    original_png: str,
    preview_png: str,
    current_html: str,
    model: str,
) -> tuple[str | None, str]:
    """Send both screenshots + the current HTML to OpenAI and get corrected HTML.

    Returns (html_or_None, error_message). The error_message is empty on
    success and contains a concise human-readable reason on failure so the
    caller can surface it to the UI.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY not set."

    # We intentionally use the REST endpoint directly so we don't force a new
    # dependency on the openai SDK — requests is already in the project.
    import requests

    orig_b64 = _b64_image(original_png)
    prev_b64 = _b64_image(preview_png)
    if not orig_b64 or not prev_b64:
        return None, "Could not read screenshot files."

    # Keep the HTML we send lean so we don't eat through the per-minute token
    # budget in a single call. 30K is enough to give the model plenty of
    # layout context without overwhelming the rate limit.
    html_snippet = current_html
    if len(html_snippet) > _HTML_MAX_CHARS:
        html_snippet = html_snippet[:_HTML_MAX_CHARS] + "\n<!-- TRUNCATED -->"

    system_prompt = (
        "You are a senior front-end engineer fixing HTML that was scraped from "
        "a Magento Page Builder site and is about to be uploaded to Builder.io "
        "as a Custom Code block. You will receive two screenshots: the ORIGINAL "
        "live page, and the current PREVIEW rendered from the HTML. Your job is "
        "to modify the HTML so its rendered layout matches the ORIGINAL as "
        "closely as possible. Rules:\n"
        "1. Return ONLY the corrected HTML fragment inside a ```html fenced "
        "code block. No prose, no explanation.\n"
        "2. Preserve every <img>, link, heading, table, and piece of text. "
        "Do NOT drop content.\n"
        "3. Fix layout issues with inline styles AND <style> rules. Common "
        "fixes: restore flex/grid column widths, remove stray "
        "position:sticky/fixed, center rows with margin:0 auto, set explicit "
        "width:100% on containers, ensure column-groups render side-by-side, "
        "restore carousels/sliders with basic horizontal scroll fallback.\n"
        "4. PRESERVE <iframe> embeds from YouTube/Vimeo exactly as given — "
        "they are the video players the original page uses. Wrap each in "
        "a responsive 16:9 container (padding-bottom:56.25%).\n"
        "5. You MAY include <style> blocks for hover/@media/keyframe rules "
        "that can't be inlined. Do NOT add <script> tags or external "
        "stylesheet links — the target container blocks them.\n"
        "6. Keep all data-* attributes (Builder.io and Page Builder rely "
        "on them)."
    )

    user_text = (
        "ORIGINAL page screenshot first, then the PREVIEW screenshot that was "
        "rendered from the HTML below. Return only the corrected HTML.\n\n"
        "CURRENT HTML:\n" + html_snippet
    )

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 8000,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{orig_b64}",
                            "detail": _IMAGE_DETAIL,
                        },
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{prev_b64}",
                            "detail": _IMAGE_DETAIL,
                        },
                    },
                ],
            },
        ],
    }

    def _post(m: str) -> tuple[str | None, str]:
        """Try one model. Returns (html, error). Raises _QuotaExceeded on
        insufficient_quota so the caller stops trying further models."""
        payload["model"] = m
        wait = _RATE_LIMIT_INITIAL_WAIT
        last_err = ""
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                response = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(payload),
                    timeout=300,
                )
                response.raise_for_status()
                data = response.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                html = _extract_html_from_response(content)
                if html:
                    return html, ""
                return None, f"{m}: model returned non-HTML response."
            except requests.HTTPError as he:
                resp = he.response
                status = resp.status_code if resp is not None else None
                code, msg = _parse_openai_error(resp) if resp is not None else ("", "")

                if status == 429:
                    # insufficient_quota means the billing quota is exhausted —
                    # retrying doesn't help, and neither does swapping models
                    # (it's per-account). Bail out hard so the UI can tell the
                    # user to top up / check billing.
                    if code == "insufficient_quota":
                        raise _QuotaExceeded(
                            msg or "OpenAI account quota exhausted — check billing."
                        )

                    # Honor Retry-After if OpenAI tells us when to come back
                    ra = resp.headers.get("Retry-After") if resp is not None else None
                    try:
                        ra_wait = int(float(ra)) if ra else 0
                    except ValueError:
                        ra_wait = 0
                    sleep_for = min(max(wait, ra_wait), _RATE_LIMIT_MAX_WAIT)

                    if attempt < _RATE_LIMIT_RETRIES:
                        logger.warning(
                            "OpenAI %s rate-limited (429%s) — retry in %ds "
                            "(attempt %d/%d). %s",
                            m, f" {code}" if code else "",
                            sleep_for, attempt + 1, _RATE_LIMIT_RETRIES, msg,
                        )
                        time.sleep(sleep_for)
                        wait *= 2
                        last_err = f"{code or 'rate_limit'}: {msg}" if msg else "rate-limited"
                        continue
                    last_err = (
                        f"{m}: rate-limited after {_RATE_LIMIT_RETRIES} retries"
                        + (f" ({msg})" if msg else "")
                    )
                    logger.warning("OpenAI %s gave up after retries: %s", m, last_err)
                    return None, last_err

                # 400/404 = model not available; surface the real reason
                last_err = f"{m}: HTTP {status}" + (f" — {msg}" if msg else f" — {he}")
                logger.warning("OpenAI %s failed (%s).", m, last_err)
                return None, last_err
            except Exception as e:
                last_err = f"{m}: {type(e).__name__}: {e}"
                logger.warning("OpenAI layout-fix request failed on %s: %s", m, e)
                return None, last_err
        return None, last_err or f"{m}: unknown failure"

    # Try models in order: user-requested → fallback → last-resort mini.
    tried: list[str] = []
    errors: list[str] = []
    for m in [model, FALLBACK_MODEL, LAST_RESORT_MODEL]:
        if m in tried:
            continue
        tried.append(m)
        try:
            html, err = _post(m)
        except _QuotaExceeded as qe:
            # Billing/quota is per-account — don't try other models.
            return None, f"OpenAI quota exhausted: {qe}"
        if html:
            return html, ""
        if err:
            errors.append(err)
        if len(tried) > 1:
            logger.info("Falling back to next model after %s failed.", m)

    return None, " | ".join(errors) or "All OpenAI models failed."


_VIDEO_IFRAME_RE = re.compile(
    r"<iframe[^>]+src\s*=\s*['\"][^'\"]*"
    r"(?:youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com|"
    r"player\.vimeo\.com|bilibili\.com|dailymotion\.com|wistia\.(?:com|net))",
    re.IGNORECASE,
)


def _validate_fix(original_html: str, fixed_html: str) -> bool:
    """Sanity check the LLM response before accepting it.

    Requirements:
    - Parses as HTML.
    - ≥ 40 % of the input length (so the model didn't silently drop the body).
    - No <script> tags.
    - iframes, if present, must point at a known video embed host. Any other
      iframe (chat widgets, trackers) is rejected.
    """
    if not fixed_html or len(fixed_html) < max(200, int(len(original_html) * 0.4)):
        logger.warning("LLM fix rejected: output too short (%d vs %d input)",
                       len(fixed_html or ""), len(original_html))
        return False

    low = fixed_html.lower()
    if "<script" in low:
        logger.warning("LLM fix rejected: introduced <script>.")
        return False

    # Walk every iframe in the output and reject the whole fix if any is not
    # a whitelisted video embed.
    if "<iframe" in low:
        try:
            from bs4 import BeautifulSoup as _BS
            _soup = _BS(fixed_html, "html.parser")
            for ifr in _soup.find_all("iframe"):
                src = (ifr.get("src") or "").strip()
                if not src or not _VIDEO_IFRAME_RE.search(f'<iframe src="{src}">'):
                    logger.warning("LLM fix rejected: non-video iframe src=%r", src)
                    return False
        except Exception as e:
            logger.warning("LLM fix rejected: iframe check failed (%s)", e)
            return False

    try:
        from bs4 import BeautifulSoup
        BeautifulSoup(fixed_html, "html.parser")
    except Exception as e:
        logger.warning("LLM fix rejected: HTML did not parse (%s)", e)
        return False

    return True


async def _fix_layout_async(
    original_url: str,
    current_html: str,
    url_key: str,
    model: str,
) -> str:
    """Async core. Returns the (possibly fixed) HTML string."""
    work_dir = Path(tempfile.mkdtemp(prefix="llm_layout_"))
    original_png = str(work_dir / f"{url_key or 'page'}_original.png")
    preview_png = str(work_dir / f"{url_key or 'page'}_preview.png")

    try:
        ok_orig = await _screenshot_url(original_url, original_png)
        if not ok_orig:
            logger.info("Skipping LLM layout fix — could not screenshot original.")
            return current_html

        ok_prev = await _screenshot_html(current_html, preview_png)
        if not ok_prev:
            logger.info("Skipping LLM layout fix — could not render preview HTML.")
            return current_html

        fixed, err = _call_openai_vision(
            original_png=original_png,
            preview_png=preview_png,
            current_html=current_html,
            model=model,
        )

        if not fixed:
            logger.info(
                "LLM returned no usable HTML — keeping original processed output. %s",
                err or "",
            )
            return current_html

        if not _validate_fix(current_html, fixed):
            return current_html

        logger.info("LLM layout fix accepted for %s", url_key or original_url)
        return fixed
    finally:
        # Clean up screenshots but keep the directory if something was saved
        for path in (original_png, preview_png):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            work_dir.rmdir()
        except OSError:
            pass


def fix_layout(
    original_url: str,
    current_html: str,
    url_key: str = "",
    model: str | None = None,
) -> str:
    """Synchronous entry point. Returns the (possibly fixed) HTML string.

    Silently returns `current_html` unchanged whenever the fixer is disabled,
    Playwright is missing, the LLM call fails, or the response is rejected by
    validation. Never raises to the caller — layout fixing is best-effort.
    """
    if not is_enabled():
        return current_html
    if not _check_playwright():
        logger.info("Playwright not installed — LLM layout fixer unavailable.")
        return current_html
    if not current_html or not original_url:
        return current_html

    model_name = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)

    try:
        return asyncio.run(
            _fix_layout_async(
                original_url=original_url,
                current_html=current_html,
                url_key=url_key,
                model=model_name,
            )
        )
    except RuntimeError:
        # Already inside an event loop (e.g. called from Streamlit): run in a
        # dedicated thread so we don't clash with the outer loop.
        import threading

        result: dict = {"html": current_html}

        def _worker() -> None:
            try:
                result["html"] = asyncio.run(
                    _fix_layout_async(
                        original_url=original_url,
                        current_html=current_html,
                        url_key=url_key,
                        model=model_name,
                    )
                )
            except Exception as e:
                logger.warning("LLM layout fix worker failed: %s", e)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=240)
        return result["html"]
    except Exception as e:
        logger.warning("LLM layout fix skipped due to error: %s", e)
        return current_html


# ---------------------------------------------------------------------------
# Manual refinement entry point
# ---------------------------------------------------------------------------
def refine_layout_with_screenshots(
    original_url: str,
    current_html: str,
    url_key: str = "",
    model: str | None = None,
) -> dict:
    """Force-run the LLM fixer and return structured diagnostics.

    This is the hook used by the Streamlit "Refine with AI" button. It
    differs from `fix_layout`:
      - It runs even if the HTML was already marked `_html_already_processed`
        (the caller explicitly asked for the fix).
      - It returns a dict so the UI can show exactly what happened
        (screenshots captured? LLM call made? validation passed?).
    """
    out = {
        "original_url": original_url,
        "html": current_html,
        "changed": False,
        "error": "",
        "model": model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        "original_screenshot": "",
        "preview_screenshot": "",
    }

    if not is_enabled():
        out["error"] = "LLM layout fixer disabled (set OPENAI_API_KEY)."
        return out
    if not _check_playwright():
        out["error"] = "Playwright not installed — cannot take screenshots."
        return out
    if not current_html or not original_url:
        out["error"] = "Missing current_html or original_url."
        return out

    async def _run() -> None:
        work_dir = Path(tempfile.mkdtemp(prefix="llm_refine_"))
        original_png = str(work_dir / f"{url_key or 'page'}_original.png")
        preview_png = str(work_dir / f"{url_key or 'page'}_preview.png")
        ok_orig = await _screenshot_url(original_url, original_png)
        if not ok_orig:
            out["error"] = "Failed to screenshot the original page."
            return
        out["original_screenshot"] = original_png
        ok_prev = await _screenshot_html(current_html, preview_png)
        if not ok_prev:
            out["error"] = "Failed to render the preview HTML for screenshot."
            return
        out["preview_screenshot"] = preview_png
        fixed, err = _call_openai_vision(
            original_png=original_png,
            preview_png=preview_png,
            current_html=current_html,
            model=out["model"],
        )
        if not fixed:
            out["error"] = err or "Model returned no usable HTML (see logs)."
            return
        if not _validate_fix(current_html, fixed):
            out["error"] = "LLM output rejected by validator (see logs)."
            return
        out["html"] = fixed
        out["changed"] = True

    try:
        asyncio.run(_run())
    except RuntimeError:
        import threading
        def _worker() -> None:
            try:
                asyncio.run(_run())
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {e}"
        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(timeout=480)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    return out
