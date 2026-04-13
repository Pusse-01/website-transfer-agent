"""
AI-powered visual quality assurance for migrated web pages.

Uses Playwright to capture screenshots of both the original URL and the
scraped/processed HTML, then sends them to OpenAI GPT-4o Vision for a
pixel-level comparison.  The model returns a list of visual differences
and a ready-to-use CSS block that fixes them.

If Playwright is not installed, falls back to HTML-text comparison with
GPT-4o (no screenshots, but still useful for structural issues).

Typical usage:
    from src.visual_qa import VisualQA
    qa = VisualQA(openai_api_key="sk-...")
    result = qa.compare(original_url, scraped_html)
    fixed_html = qa.apply_fixes(scraped_html, result["additional_css"])
"""

import base64
import json
import logging
import os
import re
import tempfile
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies — load lazily so missing packages don't crash the app
# ---------------------------------------------------------------------------
PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    pass

OPENAI_AVAILABLE = False
try:
    import openai  # type: ignore
    OPENAI_AVAILABLE = True
except Exception:
    pass


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------

def _screenshot_url(url: str, viewport_width: int = 1440, viewport_height: int = 900) -> Optional[bytes]:
    """Capture a full-page PNG screenshot of *url* using headless Chromium."""
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=VizDisplayCompositor",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-HK",
                extra_http_headers={"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"},
                java_script_enabled=True,
                bypass_csp=True,
            )
            page = ctx.new_page()
            # Hide automation flag
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});"
                "Object.defineProperty(navigator, 'languages', {get: () => ['zh-HK','zh','en']});"
            )
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception:
                # If networkidle times out, try domcontentloaded
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Dismiss cookie consent banners
            for dismiss_sel in [
                ".cookie-notice .action-dismiss",
                ".cookie-consent button",
                "#cookie-accept",
                ".accept-cookies",
                "button[data-role='accept-btn']",
            ]:
                try:
                    page.click(dismiss_sel, timeout=800)
                    page.wait_for_timeout(300)
                    break
                except Exception:
                    pass

            # Progressive scroll to trigger lazy-loading of ALL sections
            page.wait_for_timeout(2000)
            page.evaluate("""
                async function scrollFull() {
                    const totalH = document.body.scrollHeight;
                    for (let y = 0; y < totalH; y += 600) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 200));
                    }
                    window.scrollTo(0, 0);
                }
                scrollFull();
            """)
            page.wait_for_timeout(3000)

            data = page.screenshot(full_page=True, type="png")
            browser.close()
            return data
    except Exception as exc:
        logger.warning("Playwright URL screenshot failed (%s): %s", url, exc)
        return None


def _screenshot_html(
    html: str,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    base_url: str = "",
) -> Optional[bytes]:
    """Render *html* in a headless browser and return a PNG screenshot."""
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as fh:
            # Inject base tag so relative assets (fonts, images) resolve
            if base_url:
                html_out = html.replace(
                    "<head>", f'<head><base href="{base_url}">', 1
                )
                if "<head>" not in html:
                    html_out = f'<base href="{base_url}">' + html
            else:
                html_out = html
            fh.write(html_out)
            tmp_path = fh.name

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                ctx = browser.new_context(
                    viewport={"width": viewport_width, "height": viewport_height}
                )
                page = ctx.new_page()
                page.goto(f"file://{tmp_path}", wait_until="networkidle", timeout=30_000)
                page.wait_for_timeout(1500)
                data = page.screenshot(full_page=True, type="png")
                browser.close()
                return data
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        logger.warning("Playwright HTML screenshot failed: %s", exc)
        return None


def _crop_screenshot(png: bytes, max_height_px: int = 12000) -> bytes:
    """Scale down very tall screenshots to keep them within OpenAI API size limits.

    We no longer hard-crop at 4000px (which was cutting off the product section).
    Instead we allow up to 12 000 px height, and only SCALE DOWN (not crop) if
    the image is taller than that, so no content is lost.
    """
    try:
        from PIL import Image  # type: ignore
        import io

        img = Image.open(io.BytesIO(png))
        w, h = img.size
        if h > max_height_px:
            # Scale proportionally so nothing is cropped
            ratio = max_height_px / h
            new_w = max(1, int(w * ratio))
            img = img.resize((new_w, max_height_px), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return png


# ---------------------------------------------------------------------------
# OpenAI comparison helpers
# ---------------------------------------------------------------------------

_VISION_PROMPT = """\
You are a senior front-end engineer performing a visual regression audit.

You are given TWO screenshots:
• Image 1 – the ORIGINAL live website (ground truth)
• Image 2 – the MIGRATED / SCRAPED version that was rebuilt from scraped HTML

Your job:
1. Spot every visual difference (layout, colours, spacing, fonts, components).
2. Return a JSON object – and ONLY a JSON object (no markdown fences) – with this exact schema:

{
  "summary": "<2-4 sentence plain-English summary of the most important differences>",
  "differences": [
    {
      "element": "<what component / section is affected>",
      "issue": "<what is wrong in the migrated version>",
      "fix": "<specific CSS declaration(s) that would fix this>",
      "severity": "high | medium | low"
    }
  ],
  "additional_css": "<a single, self-contained CSS block (inside a <style> tag is fine) that, when injected into the migrated HTML, makes it match the original as closely as possible>"
}

Focus especially on:
- Table-of-Contents floating boxes losing their background/border
- Product carousels collapsing into vertical numbered lists
- Column layouts (side-by-side text + image) that lost their flex/grid
- Missing background colours or images
- Font weight / size differences
- Spacing / padding regressions
- Any Magento Page Builder component that looks broken
"""

_TEXT_PROMPT = """\
You are a senior front-end engineer performing a CSS regression audit.

ORIGINAL HTML (live website, truncated):
-----
{original_html}
-----

MIGRATED HTML (rebuilt from scraping, truncated):
-----
{scraped_html}
-----

The migrated version lost CSS because external stylesheets were stripped during migration
from Magento to Builder.io.  Compare the two HTML structures and return ONLY a JSON
object (no markdown) with this schema:

{{
  "summary": "<2-4 sentence summary>",
  "differences": [
    {{
      "element": "<component>",
      "issue": "<what is broken>",
      "fix": "<CSS fix>",
      "severity": "high | medium | low"
    }}
  ],
  "additional_css": "<complete CSS block that fixes all issues>"
}}

Pay special attention to:
- Product listing grids vs numbered lists
- TOC / table-of-contents boxes losing styling
- Flex/grid column layouts collapsing
- Missing Magento Page Builder component styles
"""


def _call_openai_vision(
    orig_png: bytes,
    scraped_png: bytes,
    api_key: str,
    model: str,
) -> dict:
    client = openai.OpenAI(api_key=api_key)  # type: ignore[union-attr]

    # Crop very tall screenshots to stay within token limits
    orig_cropped = _crop_screenshot(orig_png, max_height_px=4000)
    scraped_cropped = _crop_screenshot(scraped_png, max_height_px=4000)

    orig_b64 = base64.b64encode(orig_cropped).decode()
    scraped_b64 = base64.b64encode(scraped_cropped).decode()

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
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
                            "url": f"data:image/png;base64,{scraped_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


def _call_openai_text(
    original_html: str,
    scraped_html: str,
    api_key: str,
    model: str,
) -> dict:
    client = openai.OpenAI(api_key=api_key)  # type: ignore[union-attr]

    max_len = 12_000
    prompt = _TEXT_PROMPT.format(
        original_html=original_html[:max_len],
        scraped_html=scraped_html[:max_len],
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# CSS fix application
# ---------------------------------------------------------------------------

def apply_css_fixes(html: str, additional_css: str, force_important: bool = True) -> str:
    """Inject *additional_css* into *html* so the fixes take effect.

    Args:
        force_important: When True (default), append !important to every CSS
            declaration that doesn't already have it.  This is necessary because
            the processed HTML uses premailer-inlined styles (``style=""``
            attributes) which have higher specificity than class selectors in a
            ``<style>`` block.  Adding !important overrides them.
    """
    if not additional_css or not additional_css.strip():
        return html

    # Strip wrapping <style>…</style> if the model returned them
    css_only = re.sub(r"^\s*<style[^>]*>", "", additional_css.strip(), flags=re.IGNORECASE)
    css_only = re.sub(r"</style>\s*$", "", css_only.strip(), flags=re.IGNORECASE)
    css_only = css_only.strip()

    if force_important:
        # Add !important to every property declaration that lacks it.
        # Pattern: matches "property: value;" (without !important)
        css_only = re.sub(
            r'(:\s*[^;{}]+?)(\s*;)',
            lambda m: m.group(1) + " !important" + m.group(2)
            if "!important" not in m.group(1)
            else m.group(0),
            css_only,
        )

    style_block = (
        "<style>\n"
        "/* AI Visual QA fixes — !important overrides inline styles */\n"
        f"{css_only}\n"
        "</style>\n"
    )

    soup = BeautifulSoup(html, "html.parser")
    # Insert at the END of <head> so it wins specificity battles
    head = soup.find("head")
    if head:
        head.append(BeautifulSoup(style_block, "html.parser"))
    else:
        body = soup.find("body")
        if body:
            body.insert(0, BeautifulSoup(style_block, "html.parser"))
        else:
            return style_block + html

    return str(soup)


# ---------------------------------------------------------------------------
# Main VisualQA class
# ---------------------------------------------------------------------------

class VisualQA:
    """Orchestrates screenshot capture + OpenAI comparison + CSS patching."""

    def __init__(self, openai_api_key: str = "", model: str = "gpt-4o"):
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compare(
        self,
        original_url: str,
        scraped_html: str,
        source_base_url: str = "",
        use_screenshots: bool = True,
    ) -> dict:
        """Compare *original_url* against *scraped_html* using OpenAI.

        Returns a dict with keys:
            summary        – plain-English summary of differences
            differences    – list of {element, issue, fix, severity}
            additional_css – CSS block to inject
            method         – "vision" | "text" | "none"
            error          – error string if something failed (may be absent)

        If neither Playwright nor OpenAI are available the method returns
        ``{"method": "none", "differences": [], ...}`` so callers can
        still show the side-by-side preview without crashing.
        """
        if not self.openai_api_key:
            return {
                "method": "none",
                "summary": "No OpenAI API key configured.",
                "differences": [],
                "additional_css": "",
            }

        if not OPENAI_AVAILABLE:
            return {
                "method": "none",
                "summary": "openai Python package not installed.",
                "differences": [],
                "additional_css": "",
                "error": "Run: pip install openai",
            }

        # ── Try vision comparison (needs Playwright) ──────────────────
        if use_screenshots and PLAYWRIGHT_AVAILABLE:
            orig_png = _screenshot_url(original_url)
            scraped_png = _screenshot_html(scraped_html, base_url=source_base_url)
            if orig_png and scraped_png:
                try:
                    result = _call_openai_vision(
                        orig_png, scraped_png, self.openai_api_key, self.model
                    )
                    result["method"] = "vision"
                    result.setdefault("orig_screenshot", orig_png)
                    result.setdefault("scraped_screenshot", scraped_png)
                    return result
                except Exception as exc:
                    logger.warning("OpenAI vision call failed, falling back to text: %s", exc)

        # ── Fallback: text / HTML comparison ─────────────────────────
        original_html = _fetch_original_html(original_url)
        try:
            result = _call_openai_text(
                original_html, scraped_html, self.openai_api_key, self.model
            )
            result["method"] = "text"
            return result
        except Exception as exc:
            logger.error("OpenAI text comparison failed: %s", exc)
            return {
                "method": "none",
                "summary": f"AI comparison failed: {exc}",
                "differences": [],
                "additional_css": "",
                "error": str(exc),
            }

    def apply_fixes(self, html: str, additional_css: str, force_important: bool = True) -> str:
        """Return *html* with *additional_css* injected."""
        return apply_css_fixes(html, additional_css, force_important=force_important)

    def screenshot_url(self, url: str) -> Optional[bytes]:
        """Public wrapper for URL screenshot."""
        return _screenshot_url(url)

    def screenshot_html(self, html: str, base_url: str = "") -> Optional[bytes]:
        """Public wrapper for HTML screenshot."""
        return _screenshot_html(html, base_url=base_url)

    @property
    def playwright_available(self) -> bool:
        return PLAYWRIGHT_AVAILABLE

    @property
    def openai_available(self) -> bool:
        return OPENAI_AVAILABLE and bool(self.openai_api_key)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _fetch_original_html(url: str) -> str:
    """Fetch raw HTML from *url* with a browser-like User-Agent."""
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            },
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.warning("Failed to fetch original HTML from %s: %s", url, exc)
        return ""
