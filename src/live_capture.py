"""
Live page capture via Playwright.

The GraphQL / HTML scraper only returns the raw Magento content string. That
string references classes and styles defined in stylesheets that the live page
loads from its CDN — carousel CSS, hover states, slider JS, custom fonts.
When we drop that raw HTML into a Builder.io Custom Code block, those
stylesheets aren't there, so everything collapses to plain text with broken
layouts.

This module solves the root cause: it renders the live URL in Chromium, waits
for JS-driven widgets (Slick carousels, sliders, etc.) to finish initialising,
then walks the real stylesheet graph in the browser context and extracts
*only* the CSS rules that actually match elements inside the content area.
Those rules are rewritten to be scoped to a wrapper class so they can't leak
into the Builder.io chrome, concatenated into a single <style> block, and
returned together with the fully-rendered content HTML.

The result is a single self-contained HTML fragment that reproduces the live
layout 1:1 — including carousels (static snapshot of the current slide),
hover rules, responsive media queries, @font-face imports, and keyframes.

Public entry points:
    capture_live_fragment(url, content_selectors=None) -> dict
        Synchronous wrapper around the async capture.
    is_available() -> bool
        Whether Playwright is installed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Content-area selectors, tried in order. The first one that exists and has
# non-trivial text content wins. These match Magento CMS / Amasty Blog layouts.
DEFAULT_CONTENT_SELECTORS: tuple[str, ...] = (
    ".cms-page-view .column.main",
    ".cms-content",
    ".amblog-post-content",
    "article .post-content",
    ".blog-post-content",
    ".page-main .column.main",
    "#maincontent .column.main",
    "main .column.main",
    ".page-main",
    "#maincontent",
    "main",
)


@dataclass
class CaptureResult:
    """Result of a live capture run."""

    url: str = ""
    title: str = ""
    meta_title: str = ""
    meta_description: str = ""
    og_image: str = ""
    html_fragment: str = ""  # Scoped <style> + rendered content, ready for Builder.io
    images: list[str] = field(default_factory=list)
    css_rule_count: int = 0
    content_selector_used: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.html_fragment and not self.error)


def is_available() -> bool:
    """Return True if Playwright is importable."""
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# In-browser script — runs inside the rendered page and returns HTML + CSS.
# ---------------------------------------------------------------------------
#
# The script:
#   1. Finds the content root using the provided selector list.
#   2. Walks every stylesheet the page has loaded (including cross-origin
#      stylesheets exposed via CSSOM — Magento serves them same-origin so this
#      works for pricerite.com.hk).
#   3. For each rule:
#        - Style rule: keep it only if any of its selectors matches an element
#          inside the content root, OR it's a :root / html / body variables
#          rule (we always keep those so CSS custom properties resolve).
#          Rewrite the selector to be scoped under our wrapper class.
#        - Media rule: recurse — keep matched rules, wrap in @media (...) { }.
#        - @font-face / @keyframes / @supports: keep verbatim.
#   4. Returns { html, css, meta }.
#
# Designed to be resilient: if a sheet is truly cross-origin and blocked, we
# skip it and keep going instead of throwing.
_CAPTURE_SCRIPT = r"""
(({selectors_json}) => {{
  const SELECTORS = {selectors_json};

  // -------- Find content root --------
  let root = null;
  let rootSelector = "";
  for (const sel of SELECTORS) {{
    const el = document.querySelector(sel);
    if (el && el.innerText && el.innerText.trim().length > 40) {{
      root = el;
      rootSelector = sel;
      break;
    }}
  }}
  if (!root) {{
    root = document.querySelector("main") || document.body;
    rootSelector = root.tagName.toLowerCase();
  }}

  // Clone so mutations don't affect the live page before other evaluations.
  const clone = root.cloneNode(true);

  // -------- Strip chrome inside the clone (headers/footers/nav/scripts) --------
  const STRIP = [
    "script","noscript","iframe","link","meta",
    "header",".header",".page-header",".pwa-header",
    "footer",".footer",".page-footer",".pwa-footer",
    "nav",".nav",".navigation",".vertical-menu",
    ".breadcrumbs",".breadcrumbs-root-o73",
    ".modal-popup",".modal-slide",".modals-wrapper",
    ".loading-mask",".loader",
    ".minicart-wrapper",".block-search",".search-autocomplete",
    ".cookie-notice",".cookie-consent","#cookie-status",
    ".messages",".page.messages",
    ".page-title-wrapper",
    ".sidebar",".sidebar-main",".sidebar-additional",
  ];
  for (const sel of STRIP) {{
    clone.querySelectorAll(sel).forEach(n => n.remove());
  }}
  // Remove HTML comments
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_COMMENT);
  const comments = [];
  while (walker.nextNode()) comments.push(walker.currentNode);
  comments.forEach(c => c.parentNode && c.parentNode.removeChild(c));

  // -------- Collect matching CSS rules --------
  const SCOPE = ".migrated-live-content";

  // Rewrite a selector to be nested under SCOPE, but leave :root / html / body
  // rules intact (so CSS custom properties and base resets still apply to the
  // content). Split on top-level commas.
  function scopeSelector(sel) {{
    const parts = [];
    let depth = 0, buf = "";
    for (let i = 0; i < sel.length; i++) {{
      const c = sel[i];
      if (c === "(" || c === "[") depth++;
      else if (c === ")" || c === "]") depth--;
      else if (c === "," && depth === 0) {{
        parts.push(buf.trim());
        buf = "";
        continue;
      }}
      buf += c;
    }}
    if (buf.trim()) parts.push(buf.trim());

    return parts.map(p => {{
      const trimmed = p.trim();
      if (!trimmed) return "";
      // Keep :root / html / body rules at the top level so CSS variables and
      // base typography inherit into our scope. Rewrite them to target the
      // scope itself (so they're more specific than Builder.io defaults).
      if (/^(:root|html|body)(\s|$|\.|:)/i.test(trimmed)) {{
        return SCOPE;
      }}
      // Pseudo-element prefixes like ::before on the scope itself are fine.
      return SCOPE + " " + trimmed;
    }}).filter(Boolean).join(", ");
  }}

  // Does any selector match an element inside our content root?
  function matchesInside(selectorText) {{
    // Skip selectors we know can't match content elements.
    if (!selectorText) return false;
    // Test each comma-separated part independently so one bad part doesn't
    // invalidate the whole rule.
    const parts = selectorText.split(",").map(s => s.trim()).filter(Boolean);
    for (const part of parts) {{
      // Strip pseudo-elements that break querySelectorAll.
      const stripped = part.replace(/::?(?:before|after|first-line|first-letter|placeholder|marker|selection|hover|focus|focus-visible|focus-within|active|visited|checked|disabled|enabled|required|optional|valid|invalid|root)(?:\([^)]*\))?/gi, "");
      if (!stripped.trim()) continue;
      try {{
        // root itself matches? or any descendant?
        if (root.matches && root.matches(stripped)) return true;
        if (root.querySelector(stripped)) return true;
      }} catch (e) {{
        // Invalid selector — ignore
      }}
    }}
    return false;
  }}

  const collectedCss = [];
  let ruleCount = 0;

  function processRule(rule) {{
    // CSSStyleRule
    if (rule.type === 1) {{
      if (matchesInside(rule.selectorText)) {{
        const scoped = scopeSelector(rule.selectorText);
        if (scoped) {{
          // rule.cssText is "selector { body }"; swap the selector.
          const bodyMatch = rule.cssText.match(/\{([\s\S]*)\}\s*$/);
          const body = bodyMatch ? bodyMatch[1] : "";
          collectedCss.push(scoped + " {" + body + "}");
          ruleCount++;
        }}
      }}
      return;
    }}
    // CSSMediaRule / CSSSupportsRule
    if (rule.type === 4 || rule.type === 12) {{
      const inner = [];
      for (const sub of rule.cssRules || []) {{
        if (sub.type === 1) {{
          if (matchesInside(sub.selectorText)) {{
            const scoped = scopeSelector(sub.selectorText);
            if (scoped) {{
              const bodyMatch = sub.cssText.match(/\{([\s\S]*)\}\s*$/);
              const body = bodyMatch ? bodyMatch[1] : "";
              inner.push(scoped + " {" + body + "}");
              ruleCount++;
            }}
          }}
        }} else {{
          // nested @keyframes etc — keep verbatim
          inner.push(sub.cssText);
        }}
      }}
      if (inner.length > 0) {{
        const cond = rule.conditionText || (rule.media && rule.media.mediaText) || "";
        const at = rule.type === 4 ? "@media" : "@supports";
        collectedCss.push(at + " " + cond + " {\n" + inner.join("\n") + "\n}");
      }}
      return;
    }}
    // @font-face (5), @keyframes (7), @import (3), @page (6)
    if (rule.type === 5 || rule.type === 7 || rule.type === 6) {{
      collectedCss.push(rule.cssText);
      return;
    }}
    // @import — we can't inline the target synchronously; the browser has
    // already loaded it as a separate sheet, so it'll appear in styleSheets.
    // Skip here to avoid duplication.
  }}

  for (const sheet of document.styleSheets) {{
    let rules;
    try {{
      rules = sheet.cssRules || sheet.rules;
    }} catch (e) {{
      // Cross-origin blocked — skip.
      continue;
    }}
    if (!rules) continue;
    for (const rule of rules) {{
      try {{
        processRule(rule);
      }} catch (e) {{
        // Malformed rule — skip, keep going.
      }}
    }}
  }}

  // -------- Image collection --------
  const images = [];
  clone.querySelectorAll("img").forEach(img => {{
    const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
    if (src && !images.includes(src)) images.push(src);
    // Normalise src to absolute so Python side doesn't have to.
    if (src && img.getAttribute("src") !== src) img.setAttribute("src", src);
  }});

  // -------- Meta --------
  const ogImageEl = document.querySelector('meta[property="og:image"]');
  const descEl = document.querySelector('meta[name="description"]');
  const metaTitleEl = document.querySelector('title');

  return {{
    rootSelector: rootSelector,
    html: clone.outerHTML,
    css: collectedCss.join("\n\n"),
    ruleCount: ruleCount,
    title: (document.querySelector('h1') && document.querySelector('h1').innerText.trim()) || (metaTitleEl ? metaTitleEl.innerText : ""),
    metaTitle: metaTitleEl ? metaTitleEl.innerText : "",
    metaDescription: descEl ? descEl.getAttribute("content") || "" : "",
    ogImage: ogImageEl ? ogImageEl.getAttribute("content") || "" : "",
    images: images,
  }};
}})({selectors_json})
"""


def _build_script(selectors: tuple[str, ...]) -> str:
    import json
    return _CAPTURE_SCRIPT.replace("{selectors_json}", json.dumps(list(selectors)))


async def _capture_async(
    url: str,
    selectors: tuple[str, ...],
    timeout_ms: int,
    viewport_width: int,
    extra_wait_ms: int,
    login: dict | None = None,
) -> CaptureResult:
    from playwright.async_api import async_playwright

    result = CaptureResult(url=url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # Optional Magento admin login (kept off the hot path — public
            # pages don't need it). Only invoked when credentials are passed.
            if login and login.get("admin_url") and login.get("username") and login.get("password"):
                try:
                    await page.goto(login["admin_url"], wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.fill("input[name='login[username]']", login["username"])
                    await page.fill("input[name='login[password]']", login["password"])
                    await page.click("button.action-login")
                    # If OTP is required, wait — user should set OTP_CODE up front.
                    otp = login.get("otp")
                    if otp:
                        try:
                            await page.fill(
                                "input[name='tfa_code'], input[name='otp_code'], input[type='tel']",
                                str(otp),
                                timeout=5000,
                            )
                            await page.click("button[type='submit'], button.action-login")
                        except Exception:
                            pass
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except Exception as e:
                    logger.warning("Admin login skipped: %s", e)

            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Give JS-driven widgets (Slick, Swiper, etc.) time to bind & paint.
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
            if extra_wait_ms > 0:
                await page.wait_for_timeout(extra_wait_ms)

            # Scroll to bottom to trigger lazy-loaded images.
            try:
                await page.evaluate(
                    "async () => {"
                    "  const h = document.body.scrollHeight;"
                    "  for (let y = 0; y < h; y += 400) {"
                    "    window.scrollTo(0, y);"
                    "    await new Promise(r => setTimeout(r, 50));"
                    "  }"
                    "  window.scrollTo(0, 0);"
                    "}"
                )
            except Exception:
                pass

            script = _build_script(selectors)
            payload = await page.evaluate(script)

            result.title = (payload.get("title") or "").strip()
            result.meta_title = (payload.get("metaTitle") or "").strip()
            result.meta_description = (payload.get("metaDescription") or "").strip()
            result.og_image = (payload.get("ogImage") or "").strip()
            result.content_selector_used = payload.get("rootSelector", "")
            result.images = payload.get("images", []) or []
            result.css_rule_count = int(payload.get("ruleCount", 0) or 0)

            css = payload.get("css", "") or ""
            html = payload.get("html", "") or ""

            # Strip dangerous positioning that escapes Builder.io's Custom Code
            # container. Sticky/fixed headers inside a block float over
            # unrelated parts of the Builder page.
            css = re.sub(
                r"position\s*:\s*(sticky|fixed)\s*;?",
                "",
                css,
                flags=re.IGNORECASE,
            )

            # Final assembly: one self-contained fragment.
            result.html_fragment = (
                '<div class="migrated-live-content">\n'
                f"<style>\n{css}\n</style>\n"
                f"{html}\n"
                "</div>"
            )
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            logger.exception("Live capture failed for %s", url)
        finally:
            await context.close()
            await browser.close()

    return result


def capture_live_fragment(
    url: str,
    content_selectors: tuple[str, ...] | None = None,
    timeout_ms: int = 45000,
    viewport_width: int = 1280,
    extra_wait_ms: int = 1500,
    login: dict | None = None,
) -> CaptureResult:
    """
    Render the given URL and return a self-contained Builder.io-ready HTML
    fragment that visually mirrors the live page.

    Args:
        url: Full URL to capture.
        content_selectors: CSS selectors tried in order to locate the main
            content area. Defaults to Magento CMS + Amasty Blog layouts.
        timeout_ms: Per-navigation timeout.
        viewport_width: Simulated browser width (1280 matches the current
            desktop breakpoint on pricerite.com.hk).
        extra_wait_ms: Extra idle time after networkidle to let JS widgets
            paint (sliders, carousels, lazy renderers).
        login: Optional dict with {admin_url, username, password, otp}. Only
            needed if the target URL is *not* publicly accessible. Public CMS
            pages like /hk/zh/intro-fur-tips don't need this.

    Returns:
        CaptureResult. Check `.ok` before using.
    """
    if not is_available():
        return CaptureResult(
            url=url,
            error="Playwright is not installed. Run: pip install playwright && playwright install chromium",
        )

    selectors = content_selectors or DEFAULT_CONTENT_SELECTORS

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _capture_async(
                    url=url,
                    selectors=selectors,
                    timeout_ms=timeout_ms,
                    viewport_width=viewport_width,
                    extra_wait_ms=extra_wait_ms,
                    login=login,
                )
            )
        finally:
            loop.close()
    except Exception as e:
        logger.exception("capture_live_fragment failed")
        return CaptureResult(url=url, error=f"{type(e).__name__}: {e}")
