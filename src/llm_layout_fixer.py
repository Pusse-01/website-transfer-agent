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
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "gpt-4o"
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 800


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
    """Capture a full-page screenshot of a live URL."""
    if not _check_playwright():
        logger.warning("Playwright not installed — cannot capture original screenshot.")
        return False

    from playwright.async_api import async_playwright

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
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
            await page.screenshot(path=out_path, full_page=True)
            await browser.close()
        return True
    except Exception as e:  # pragma: no cover - network-dependent
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
            await page.goto(f"file://{html_path}", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(800)
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


def _call_openai_vision(
    original_png: str,
    preview_png: str,
    current_html: str,
    model: str,
) -> str | None:
    """Send both screenshots + the current HTML to OpenAI and get corrected HTML."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    # We intentionally use the REST endpoint directly so we don't force a new
    # dependency on the openai SDK — requests is already in the project.
    import requests

    orig_b64 = _b64_image(original_png)
    prev_b64 = _b64_image(preview_png)
    if not orig_b64 or not prev_b64:
        return None

    # Keep the HTML we send reasonable in size. Very long pages overflow the
    # context window; truncating to ~120k chars preserves structure for the
    # vast majority of real-world pages.
    html_snippet = current_html
    if len(html_snippet) > 120_000:
        html_snippet = html_snippet[:120_000] + "\n<!-- TRUNCATED -->"

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
        "3. Fix layout issues with inline styles. Common fixes: restore "
        "flex/grid column widths, remove stray position:sticky/fixed, center "
        "rows with margin:0 auto, set explicit width:100% on containers, "
        "ensure column-groups render side-by-side.\n"
        "4. Do not add <script>, <iframe>, <style>, or external stylesheet "
        "links — the target container strips them.\n"
        "5. Keep all data-* attributes (Builder.io and Page Builder rely "
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
                            "detail": "high",
                        },
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{prev_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        return _extract_html_from_response(content)
    except Exception as e:
        logger.warning("OpenAI layout-fix request failed: %s", e)
        return None


def _validate_fix(original_html: str, fixed_html: str) -> bool:
    """Sanity check the LLM response before accepting it.

    We require that:
    - It actually parses as HTML.
    - It has at least 40% of the character length of the input (so we don't
      accept an answer that silently dropped the body).
    - It doesn't introduce <script> or <iframe> tags.
    """
    if not fixed_html or len(fixed_html) < max(200, int(len(original_html) * 0.4)):
        logger.warning("LLM fix rejected: output too short (%d vs %d input)",
                       len(fixed_html or ""), len(original_html))
        return False

    low = fixed_html.lower()
    if "<script" in low or "<iframe" in low:
        logger.warning("LLM fix rejected: introduced <script>/<iframe>.")
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

        fixed = _call_openai_vision(
            original_png=original_png,
            preview_png=preview_png,
            current_html=current_html,
            model=model,
        )

        if not fixed:
            logger.info("LLM returned no usable HTML — keeping original processed output.")
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
