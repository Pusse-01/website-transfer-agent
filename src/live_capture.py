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


# ---------------------------------------------------------------------------
# Carousel reinitialisation script — injected into every captured fragment.
#
# Magento pages use Slick carousel. Playwright captures the fully-rendered
# HTML (arrows, slides, track) but strips all <script> tags, so Slick's
# event listeners are gone. This script recreates the core behaviour:
#
#   • Removes Slick's cloned slides (used for infinite scroll — not needed
#     in a static Builder.io page and cause duplicate content).
#   • Switches the track from pixel-based translate (captured at 1280 px
#     viewport) to percentage-based translateX so it works at any width.
#   • Re-attaches click handlers to .slick-prev / .slick-next arrows.
#   • Works for every .slick-slider on the page (product carousels, related-
#     products rows, featured-products sections, etc.).
# ---------------------------------------------------------------------------
_CAROUSEL_REINIT_JS = """\
(function () {
  'use strict';

  function initCarousel(slider) {
    var list  = slider.querySelector('.slick-list');
    var track = list && list.querySelector('.slick-track');
    if (!list || !track) return;

    /* ---- collect real slides, discard Slick-generated clones ----------- */
    var allSlides  = Array.from(track.querySelectorAll('.slick-slide'));
    var realSlides = allSlides.filter(function (s) {
      return !s.classList.contains('slick-cloned');
    });
    if (realSlides.length === 0) return;

    /* ---- how many slides are shown at once? --------------------------------
       Priority order:
         1. data-slick JSON  (Magento Page Builder sets this, e.g. slidesToShow:4)
         2. data-pc-carousel-count  (another Magento Page Builder attribute)
         3. Count of .slick-active real slides (Slick marks all visible ones)
    -------------------------------------------------------------------------- */
    var slidesToShow = 1;

    /* 1. data-slick JSON attribute */
    var slickAttrStr = slider.getAttribute('data-slick') || '';
    if (slickAttrStr) {
      try {
        var slickCfg = JSON.parse(slickAttrStr);
        if (slickCfg && typeof slickCfg.slidesToShow === 'number' && slickCfg.slidesToShow > 0) {
          slidesToShow = slickCfg.slidesToShow;
        }
      } catch (jsonErr) { /* malformed JSON — ignore */ }
    }

    /* 2. data-pc-carousel-count */
    if (slidesToShow <= 1) {
      var pcCount = parseInt(slider.getAttribute('data-pc-carousel-count') || '0', 10);
      if (!isNaN(pcCount) && pcCount > 1) {
        slidesToShow = pcCount;
      }
    }

    /* 3. Count .slick-active real slides as last resort */
    if (slidesToShow <= 1) {
      var activeSlides = realSlides.filter(function (s) {
        return s.classList.contains('slick-active');
      });
      if (activeSlides.length > 1) {
        slidesToShow = activeSlides.length;
      }
    }

    /* ---- remove clones ------------------------------------------------- */
    allSlides.forEach(function (s) {
      if (s.classList.contains('slick-cloned')) {
        s.parentNode && s.parentNode.removeChild(s);
      }
    });

    /* ---- reset every slide to percentage width ------------------------- */
    var pct = (100 / slidesToShow) + '%';
    realSlides.forEach(function (slide) {
      slide.style.width      = pct;
      slide.style.flexShrink = '0';
      slide.style.display    = 'block';
      slide.removeAttribute('aria-hidden');
    });

    /* ---- reset track: flex + percentage translate ---------------------- */
    track.style.cssText = [
      'display: flex',
      'flex-wrap: nowrap',
      'transition: transform 0.35s ease',
      'will-change: transform',
      'width: 100%'
    ].join('; ') + ';';

    list.style.overflow = 'hidden';
    list.style.position = 'relative';
    list.style.width    = '100%';

    /* ---- state --------------------------------------------------------- */
    var currentIndex = 0;
    var maxIndex     = Math.max(0, realSlides.length - slidesToShow);

    function goTo(n) {
      currentIndex = Math.max(0, Math.min(maxIndex, n));
      track.style.transform =
        'translateX(-' + (currentIndex * (100 / slidesToShow)) + '%)';
    }

    /* ---- wire arrow buttons ------------------------------------------- */
    function bindArrow(selector, delta) {
      var btn = slider.querySelector(selector);
      if (!btn) return;
      /* Replace node to drop any stale handlers left by Slick */
      var fresh = btn.cloneNode(true);
      btn.parentNode && btn.parentNode.replaceChild(fresh, btn);
      fresh.style.cursor        = 'pointer';
      fresh.style.pointerEvents = 'auto';
      fresh.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        goTo(currentIndex + delta);
      });
    }

    bindArrow('.slick-prev', -1);
    bindArrow('.slick-next', +1);

    goTo(0);
  }

  function run() {
    document.querySelectorAll('.slick-slider').forEach(function (slider) {
      try { initCarousel(slider); } catch (err) { /* silent */ }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    /* DOMContentLoaded already fired (common inside Builder.io) */
    setTimeout(run, 0);
  }
})();
"""


# Extra CSS added to every live-captured fragment to fix carousel layout
# and prevent image cropping that happens when Slick's pixel-based widths
# are no longer valid at Builder.io's viewport.
_CAROUSEL_CSS_FIXES = """\
/* ---- Layout and carousel fixes (injected by migration agent) ----------- */

/* Ensure migrated content fills the Builder.io section width */
.migrated-live-content {
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
}

/* ---- Slick carousel overrides ----------------------------------------- */
.migrated-live-content .slick-list {
  overflow: hidden !important;
  position: relative;
  width: 100%;
}
.migrated-live-content .slick-track {
  display: flex !important;
  flex-wrap: nowrap;
}
.migrated-live-content .slick-slide {
  flex-shrink: 0;
  min-width: 0;
  box-sizing: border-box;
  /* Override Slick's captured pixel width (from 1280px render) so slides
     don't overflow before the carousel reinit JS runs. The JS then sets
     slide.style.width = percentage as an inline style, which takes
     precedence over this declaration. */
  width: auto;
}
.migrated-live-content .slick-slide > div {
  height: 100%;
}
/* Carousel images: fill slide width without overriding height.
   IMPORTANT: do NOT set height:auto here — Magento product images use
   position:absolute + height:100% inside a padding-bottom aspect-ratio
   container. Forcing height:auto breaks that layout and hides images. */
.migrated-live-content .slick-slide img {
  width: 100%;
  max-width: 100%;
  display: block;
}
/* Arrow buttons must remain visible and clickable inside Builder.io */
.migrated-live-content .slick-arrow {
  cursor: pointer;
  z-index: 10;
  pointer-events: auto !important;
}
"""


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
(() => {
  const SELECTORS = {selectors_json};

  // -------- Find content root --------
  let root = null;
  let rootSelector = "";
  for (const sel of SELECTORS) {
    const el = document.querySelector(sel);
    if (el && el.innerText && el.innerText.trim().length > 40) {
      root = el;
      rootSelector = sel;
      break;
    }
  }
  if (!root) {
    root = document.querySelector("main") || document.body;
    rootSelector = root.tagName.toLowerCase();
  }

  // Normalise relative image src attributes on LIVE elements *before* cloning.
  // The DOM property `img.src` (without getAttribute) always returns the
  // fully-resolved absolute URL for a live element in the document, whereas
  // `img.getAttribute("src")` returns the raw HTML attribute which may be a
  // relative Magento media path like "catalog/product/9/7/image.jpg".
  // After this step the clone will inherit absolute src attributes, so we
  // no longer need to rely on `img.currentSrc` (which is empty on clones).
  root.querySelectorAll("img").forEach(function(liveImg) {
    var absSrc = liveImg.src;  // DOM property — always absolute for live elements
    if (absSrc && !absSrc.startsWith("data:") && liveImg.getAttribute("src") !== absSrc) {
      liveImg.setAttribute("src", absSrc);
    }
  });

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
  for (const sel of STRIP) {
    clone.querySelectorAll(sel).forEach(n => n.remove());
  }
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
  function scopeSelector(sel) {
    const parts = [];
    let depth = 0, buf = "";
    for (let i = 0; i < sel.length; i++) {
      const c = sel[i];
      if (c === "(" || c === "[") depth++;
      else if (c === ")" || c === "]") depth--;
      else if (c === "," && depth === 0) {
        parts.push(buf.trim());
        buf = "";
        continue;
      }
      buf += c;
    }
    if (buf.trim()) parts.push(buf.trim());

    return parts.map(p => {
      const trimmed = p.trim();
      if (!trimmed) return "";
      // Keep :root / html / body rules at the top level so CSS variables and
      // base typography inherit into our scope. Rewrite them to target the
      // scope itself (so they're more specific than Builder.io defaults).
      if (/^(:root|html|body)(\s|$|\.|:)/i.test(trimmed)) {
        return SCOPE;
      }
      // Pseudo-element prefixes like ::before on the scope itself are fine.
      return SCOPE + " " + trimmed;
    }).filter(Boolean).join(", ");
  }

  // Does any selector match an element inside our content root?
  function matchesInside(selectorText) {
    // Skip selectors we know can't match content elements.
    if (!selectorText) return false;
    // Test each comma-separated part independently so one bad part doesn't
    // invalidate the whole rule.
    const parts = selectorText.split(",").map(s => s.trim()).filter(Boolean);
    for (const part of parts) {
      // Strip pseudo-elements that break querySelectorAll.
      const stripped = part.replace(/::?(?:before|after|first-line|first-letter|placeholder|marker|selection|hover|focus|focus-visible|focus-within|active|visited|checked|disabled|enabled|required|optional|valid|invalid|root)(?:\([^)]*\))?/gi, "");
      if (!stripped.trim()) continue;
      try {
        // root itself matches? or any descendant?
        if (root.matches && root.matches(stripped)) return true;
        if (root.querySelector(stripped)) return true;
      } catch (e) {
        // Invalid selector — ignore
      }
    }
    return false;
  }

  const collectedCss = [];
  let ruleCount = 0;

  function processRule(rule) {
    // CSSStyleRule
    if (rule.type === 1) {
      if (matchesInside(rule.selectorText)) {
        const scoped = scopeSelector(rule.selectorText);
        if (scoped) {
          // rule.cssText is "selector { body }"; swap the selector.
          const bodyMatch = rule.cssText.match(/\{([\s\S]*)\}\s*$/);
          const body = bodyMatch ? bodyMatch[1] : "";
          collectedCss.push(scoped + " {" + body + "}");
          ruleCount++;
        }
      }
      return;
    }
    // CSSMediaRule / CSSSupportsRule
    if (rule.type === 4 || rule.type === 12) {
      const inner = [];
      for (const sub of rule.cssRules || []) {
        if (sub.type === 1) {
          if (matchesInside(sub.selectorText)) {
            const scoped = scopeSelector(sub.selectorText);
            if (scoped) {
              const bodyMatch = sub.cssText.match(/\{([\s\S]*)\}\s*$/);
              const body = bodyMatch ? bodyMatch[1] : "";
              inner.push(scoped + " {" + body + "}");
              ruleCount++;
            }
          }
        } else {
          // nested @keyframes etc — keep verbatim
          inner.push(sub.cssText);
        }
      }
      if (inner.length > 0) {
        const cond = rule.conditionText || (rule.media && rule.media.mediaText) || "";
        const at = rule.type === 4 ? "@media" : "@supports";
        collectedCss.push(at + " " + cond + " {\n" + inner.join("\n") + "\n}");
      }
      return;
    }
    // @font-face (5), @keyframes (7), @import (3), @page (6)
    if (rule.type === 5 || rule.type === 7 || rule.type === 6) {
      collectedCss.push(rule.cssText);
      return;
    }
    // @import — we can't inline the target synchronously; the browser has
    // already loaded it as a separate sheet, so it'll appear in styleSheets.
    // Skip here to avoid duplication.
  }

  for (const sheet of document.styleSheets) {
    let rules;
    try {
      rules = sheet.cssRules || sheet.rules;
    } catch (e) {
      // Cross-origin blocked — skip.
      continue;
    }
    if (!rules) continue;
    for (const rule of rules) {
      try {
        processRule(rule);
      } catch (e) {
        // Malformed rule — skip, keep going.
      }
    }
  }

  // -------- Image collection --------
  // Slick carousel (and other lazy loaders) set src to a tiny blank data: URI
  // placeholder while the real URL is stored in data-src / data-lazy /
  // data-original. We must prefer the real URL so the migration agent can
  // upload the correct image to Builder.io.
  const images = [];
  clone.querySelectorAll("img").forEach(img => {
    const rawSrc  = img.getAttribute("src") || "";
    const lazySrc = img.getAttribute("data-src")
                 || img.getAttribute("data-lazy")
                 || img.getAttribute("data-original")
                 || "";
    // Prefer lazySrc when src is a data: URI placeholder.
    // NOTE: img.currentSrc is always empty on detached clone nodes — do NOT
    // rely on it. The live-element normalization above ensures rawSrc is
    // already an absolute URL for anything that was in the live DOM.
    const src = (rawSrc.startsWith("data:") && lazySrc)
      ? lazySrc
      : (rawSrc || lazySrc);
    if (src && !images.includes(src)) images.push(src);
    // Ensure the clone's src attribute is the resolved absolute URL.
    if (src && img.getAttribute("src") !== src) img.setAttribute("src", src);
    // Remove lazy-load attributes so Builder.io renders the image immediately
    img.removeAttribute("data-src");
    img.removeAttribute("data-lazy");
    img.removeAttribute("data-original");
    if (img.getAttribute("loading") === "lazy") img.setAttribute("loading", "eager");
  });

  // -------- Meta --------
  const ogImageEl = document.querySelector('meta[property="og:image"]');
  const descEl = document.querySelector('meta[name="description"]');
  const metaTitleEl = document.querySelector('title');

  return {
    rootSelector: rootSelector,
    html: clone.outerHTML,
    css: collectedCss.join("\n\n"),
    ruleCount: ruleCount,
    title: (document.querySelector('h1') && document.querySelector('h1').innerText.trim()) || (metaTitleEl ? metaTitleEl.innerText : ""),
    metaTitle: metaTitleEl ? metaTitleEl.innerText : "",
    metaDescription: descEl ? descEl.getAttribute("content") || "" : "",
    ogImage: ogImageEl ? ogImageEl.getAttribute("content") || "" : "",
    images: images,
  };
})()
"""


def _build_script(selectors: tuple[str, ...]) -> str:
    """Render the in-browser capture script.

    The source script uses single JS braces throughout (not doubled for
    Python `.format()`), and `{selectors_json}` is substituted via a plain
    `.replace()`. An earlier version had doubled braces `{{` / `}}` which
    — because `.replace()` doesn't un-escape them — produced invalid JS
    (the browser raised `SyntaxError: Invalid destructuring assignment
    target` and Playwright silently fell back to the legacy scraper,
    gutting the whole fidelity pipeline).
    """
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

            # Force-load lazy images before capturing.
            # Slick carousel shows only the active slides; the remaining
            # slides have src="data:..." placeholders with the real URL in
            # data-src. We set src from data-src so that:
            #   (a) img.currentSrc is populated when the capture script runs,
            #   (b) the image_handler can upload all images, not just active ones.
            try:
                await page.evaluate(
                    "() => {"
                    "  document.querySelectorAll('img').forEach(function(img) {"
                    "    var lazy = img.getAttribute('data-src')"
                    "           || img.getAttribute('data-lazy')"
                    "           || img.getAttribute('data-original');"
                    "    var s = img.getAttribute('src') || '';"
                    "    if (lazy && (s.startsWith('data:') || !s)) {"
                    "      img.src = lazy;"
                    "    }"
                    "  });"
                    "}"
                )
                # Give the browser a moment to start loading the newly-set srcs.
                await page.wait_for_timeout(800)
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
            # Append carousel CSS fixes to the captured stylesheet so they
            # override any conflicting Slick rules already in `css`.
            # The carousel reinit script re-wires arrow buttons and converts
            # Slick's pixel-based layout to percentage-based so it works at
            # any viewport width inside Builder.io.
            result.html_fragment = (
                '<div class="migrated-live-content">\n'
                f"<style>\n{css}\n{_CAROUSEL_CSS_FIXES}\n</style>\n"
                f"<script>\n{_CAROUSEL_REINIT_JS}\n</script>\n"
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
