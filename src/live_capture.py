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


def _convert_rem_to_px(css: str, root_px: float) -> str:
    """Convert rem values in CSS rule bodies to px using the captured root font size.

    Leaves @media/@supports/@container condition parts (before the first '{')
    untouched — those are always evaluated against the browser initial value of
    16 px regardless of html { font-size: … }, so they are already correct on
    both the original site and inside Builder.io.

    This fixes the common Magento/PWA-Studio pattern where the theme sets
    html { font-size: 62.5% } (= 10 px) and sizes everything in rem.  Inside
    Builder.io the html element stays at 16 px, so 1.4rem renders as 22.4 px
    instead of the intended 14 px.  Converting to absolute px eliminates the
    dependency on the host-page root font size.
    """
    if abs(root_px - 16.0) < 0.1:
        return css  # Root is effectively 16 px — rem already resolves correctly.

    # Protect @media / @supports / @container condition text (everything from
    # the at-keyword up to but NOT including the opening brace of the rule body)
    # by swapping it out for a placeholder.  rem values there must stay as rem
    # because the browser resolves them against 16 px regardless of html font-size.
    placeholders: dict[str, str] = {}
    _counter = [0]

    def _protect(m: re.Match) -> str:
        key = f"\x00ATCOND{_counter[0]}\x00"
        placeholders[key] = m.group(0)
        _counter[0] += 1
        return key

    protected = re.sub(
        r"@(?:media|supports|container)\b[^{]*",
        _protect,
        css,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _rem_to_px(m: re.Match) -> str:
        val = float(m.group(1))
        px = val * root_px
        return f"{int(px)}px" if px == int(px) else f"{round(px, 3)}px"

    converted = re.sub(r"(-?(?:\d+\.?\d*|\.\d+))\s*rem\b", _rem_to_px, protected)

    for key, original in placeholders.items():
        converted = converted.replace(key, original)

    return converted


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


# ---------------------------------------------------------------------------
# Tab reinitialisation script — injected alongside the carousel reinit.
#
# Magento Page Builder tab widgets and Magento native tab widgets both lose
# their click handlers when <script> tags are stripped for Builder.io Custom
# Code blocks. This script re-wires the tab buttons so clicking them shows
# the correct content pane.
#
# Handles three patterns:
#   1. Magento Page Builder  → [data-content-type="tabs"] with
#      .tab-header-item headers and [data-content-type="tab-item"] panes
#   2. Magento native widget → ul.tabs-navigation siblings of .tabs-content
#   3. ARIA tablist          → [role="tablist"] + [role="tab"] + aria-controls
# ---------------------------------------------------------------------------
_TAB_REINIT_JS = """\
(function () {
  'use strict';

  function switchTo(freshHeaders, panes, i) {
    freshHeaders.forEach(function (h, idx) {
      var on = idx === i;
      h.classList.toggle('active', on);
      h.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    panes.forEach(function (p, idx) {
      if (idx === i) {
        p.style.removeProperty('display');
        p.classList.add('active');
      } else {
        p.style.setProperty('display', 'none', '');
        p.classList.remove('active');
      }
    });
  }

  function wireHeaders(rawHeaders, panes) {
    if (!rawHeaders.length || !panes.length) return;
    /* Clone nodes to strip any stale event listeners Magento / React left. */
    var fresh = rawHeaders.map(function (h) {
      var f = h.cloneNode(true);
      h.parentNode && h.parentNode.replaceChild(f, h);
      return f;
    });
    fresh.forEach(function (h, i) {
      h.style.cursor = 'pointer';
      h.addEventListener('click', function () { switchTo(fresh, panes, i); });
    });
    /* Start with the first tab active. */
    switchTo(fresh, panes, 0);
  }

  /* ---- Pattern 1: Magento Page Builder tabs -------------------------------- */
  function initPBTabs() {
    document.querySelectorAll('[data-content-type="tabs"]').forEach(function (w) {
      if (w.__tabsOk) return;
      w.__tabsOk = true;

      /* Headers — the <ul> inside the widget contains <li> tab buttons. */
      var ul = w.querySelector(':scope > div > ul, :scope > ul');
      var headers = ul ? Array.from(ul.querySelectorAll(
        'li.tab-header-item, li[data-tab-item], li'
      )) : [];

      /* Panes — direct [data-content-type="tab-item"] children. */
      var panes = Array.from(w.querySelectorAll('[data-content-type="tab-item"]'));

      wireHeaders(headers, panes);
    });
  }

  /* ---- Pattern 2: Magento native tab widget -------------------------------- */
  function initNativeTabs() {
    document.querySelectorAll('ul.tabs-navigation, .nav-tabs, [class*="tabNav"]').forEach(
      function (nav) {
        if (nav.__tabsOk) return;
        nav.__tabsOk = true;

        var items = Array.from(nav.querySelectorAll('li, [role="tab"]'));
        /* Look for the sibling content wrapper. */
        var wrap = nav.parentNode && nav.parentNode.querySelector(
          '.tabs-content, .tab-content, [class*="tabContent"]'
        );
        if (!wrap) wrap = nav.nextElementSibling;
        if (!wrap) return;

        var panes = Array.from(wrap.querySelectorAll(
          '.tab-container, .tab-pane, [data-role="content"], [class*="tabPane"]'
        ));
        if (!panes.length) panes = Array.from(wrap.children);

        wireHeaders(items, panes);
      }
    );
  }

  /* ---- Pattern 3: ARIA tablist -------------------------------------------- */
  function initAriaTabs() {
    document.querySelectorAll('[role="tablist"]').forEach(function (tl) {
      if (tl.__tabsOk) return;
      tl.__tabsOk = true;

      var tabs = Array.from(tl.querySelectorAll('[role="tab"]'));
      var panes = tabs.map(function (t) {
        var id = t.getAttribute('aria-controls')
               || (t.getAttribute('href') || '').replace(/^#/, '')
               || t.getAttribute('data-target') || '';
        return id ? (document.getElementById(id)
               || document.querySelector('[data-panel="' + id + '"]')) : null;
      }).filter(Boolean);

      wireHeaders(tabs, panes);
    });
  }

  function run() {
    initPBTabs();
    initNativeTabs();
    initAriaTabs();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
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

/* ---- Typography baseline ---------------------------------------------
   Builder.io's Custom Code container sometimes applies bold / dark text
   rules that cascade into our content, which is why migrated pages look
   heavier than the original. Pin the baseline typography explicitly so
   our captured rules on .product-name, headings, etc. layer on top of a
   clean reset rather than on top of Builder.io's chrome.
---------------------------------------------------------------------- */
.migrated-live-content,
.migrated-live-content * {
  font-weight: inherit;
  color: inherit;
}
.migrated-live-content {
  font-family: "PingFang HK", "PingFang TC", "Noto Sans TC",
               "Microsoft JhengHei", "Helvetica Neue", Arial, sans-serif;
  font-weight: 400;
  color: #333;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
.migrated-live-content h1,
.migrated-live-content h2,
.migrated-live-content h3,
.migrated-live-content h4,
.migrated-live-content h5,
.migrated-live-content h6,
.migrated-live-content strong,
.migrated-live-content b {
  font-weight: 600;
  color: #1a1a1a;
}
.migrated-live-content p,
.migrated-live-content li,
.migrated-live-content span,
.migrated-live-content div {
  font-weight: inherit;
}

/* ---- Slick carousel overrides -----------------------------------------
   product_tile_builder.normalize_slick_carousels strips Slick's inline
   pixel widths and translate3d transforms statically, so these rules are
   the ONLY source of carousel sizing — no JS runtime required.  The
   carousel renders as a horizontally scrollable flex row that wraps to
   a stacked column on mobile. */
.migrated-live-content .slick-list {
  overflow-x: auto !important;
  overflow-y: hidden !important;
  position: relative;
  width: 100%;
  -webkit-overflow-scrolling: touch;
}
.migrated-live-content .slick-track {
  display: flex !important;
  flex-wrap: nowrap !important;
  /* Belt-and-braces in case the Python stripper missed the inline style
     (e.g. the class `slick-track` was applied by a custom theme rename). */
  transform: none !important;
  width: auto !important;
  min-width: 100%;
}
.migrated-live-content .slick-slide {
  flex: 0 0 auto !important;
  min-width: 0;
  box-sizing: border-box;
  /* Override any captured pixel width from the 1280px render. */
  width: auto !important;
  max-width: 320px;
}
.migrated-live-content .slick-slide > div {
  height: 100%;
}
/* Carousel images: contain (not cover) so product photos aren't cropped
   when the slide's aspect ratio differs from the image's.
   IMPORTANT: do NOT force height:auto on all product images — Magento's
   .product-image-photo uses position:absolute + height:100% inside a
   padding-bottom aspect-ratio box. We target plain <img> children only. */
.migrated-live-content .slick-slide img {
  max-width: 100%;
  display: block;
}
.migrated-live-content .slick-slide .product-image-photo,
.migrated-live-content .product-image-photo {
  width: 100%;
  height: 100%;
  object-fit: contain;
  object-position: center;
}
/* Arrow buttons must remain visible and clickable inside Builder.io */
.migrated-live-content .slick-arrow {
  cursor: pointer;
  z-index: 10;
  pointer-events: auto !important;
}

/* ---- Equal-height cards in Magento Page Builder column groups ---------
   The original uses `display:flex` on the row with each column as a flex
   child, so cards stretch to the tallest sibling.  Without the original
   layout JS we enforce that with plain CSS.  We also unlock any
   max-height / line-clamp that would crop text at three lines. */
.migrated-live-content [data-content-type="row"],
.migrated-live-content [data-content-type="column-group"],
.migrated-live-content .pagebuilder-column-group,
.migrated-live-content .pagebuilder-column-line {
  display: flex !important;
  flex-wrap: wrap;
  align-items: stretch !important;
}
.migrated-live-content [data-content-type="column"],
.migrated-live-content .pagebuilder-column {
  display: flex !important;
  flex-direction: column !important;
  align-items: stretch !important;
  height: auto !important;
  min-height: 0;
}
.migrated-live-content [data-content-type="column"] > *,
.migrated-live-content .pagebuilder-column > * {
  flex: 0 0 auto;
}
/* Let every card body show all its text — Magento Page Builder's default
   stylesheet clamps text-content blocks at a fixed height via overflow:
   hidden which causes the "lower part cropped out" issue. */
.migrated-live-content [data-content-type="text"],
.migrated-live-content [data-content-type="html"],
.migrated-live-content .pagebuilder-column [data-content-type="text"],
.migrated-live-content .pagebuilder-column [data-content-type="html"] {
  max-height: none !important;
  overflow: visible !important;
  -webkit-line-clamp: unset !important;
  display: block !important;
}
.migrated-live-content .pagebuilder-column p,
.migrated-live-content .pagebuilder-column h1,
.migrated-live-content .pagebuilder-column h2,
.migrated-live-content .pagebuilder-column h3,
.migrated-live-content .pagebuilder-column h4,
.migrated-live-content .pagebuilder-column h5,
.migrated-live-content .pagebuilder-column h6,
.migrated-live-content .pagebuilder-column li {
  overflow: visible !important;
  max-height: none !important;
  -webkit-line-clamp: unset !important;
  text-overflow: clip !important;
  white-space: normal !important;
}

/* ---- Mobile responsive: stack multi-column rows on small screens ------
   The original Magento Page Builder uses @media queries that turn
   multi-column rows into a vertical stack (or a Slick mobile slider) on
   viewports < 768px.  Those rules sometimes get lost in our scoped CSS,
   which leaves the narrow columns side-by-side and each column's text
   wraps one-character-per-line.  Force the stack explicitly. */
@media (max-width: 767px) {
  .migrated-live-content [data-content-type="row"],
  .migrated-live-content [data-content-type="column-group"],
  .migrated-live-content .pagebuilder-column-group,
  .migrated-live-content .pagebuilder-column-line {
    flex-direction: column !important;
  }
  .migrated-live-content [data-content-type="column"],
  .migrated-live-content .pagebuilder-column {
    width: 100% !important;
    max-width: 100% !important;
    flex-basis: auto !important;
    margin-bottom: 16px;
  }
  /* Slick carousel: keep horizontal scroll on mobile so product tiles don't
     stretch to full-width (which looks wrong for a 200px product image). */
  .migrated-live-content .slick-slide {
    max-width: 240px;
  }
}

/* ---- Tabbed content: hide inactive panes, show active pane ----------- */
.migrated-live-content [data-content-type="tab-item"] {
  display: none !important;
}
.migrated-live-content [data-content-type="tab-item"]:first-child,
.migrated-live-content [data-content-type="tab-item"].active {
  display: block !important;
}
.migrated-live-content .tab-header-item,
.migrated-live-content [role="tab"],
.migrated-live-content .tabs-navigation li a {
  cursor: pointer !important;
  user-select: none;
}
.migrated-live-content .tab-header-item.active,
.migrated-live-content [role="tab"][aria-selected="true"],
.migrated-live-content .tabs-navigation li.active a {
  font-weight: 700 !important;
}
"""


# Content-area selectors, tried in order. The first one that exists and has
# non-trivial text content wins. These match Magento CMS / Amasty Blog layouts.
#
# Order matters: the tightest, post-only wrappers come FIRST so we don't
# accidentally pick a broad container that also includes the sidebar column.
# Amasty Blog renders a post as .amblog-post-view containing the hero image
# (.amblog-post-image) AND the body (.amblog-post-content) as siblings — so we
# target the post wrapper, not just the text body, otherwise we lose the hero.
DEFAULT_CONTENT_SELECTORS: tuple[str, ...] = (
    # Pricerite / PWA Studio: blogDetail-blogPostItem wraps hero image
    # (blogDetail-ImageBox) + article body. Sidebar widgets
    # (blogDetail-normalBox) are siblings outside it, so this selector
    # captures image+content without any sidebar.
    "[class*='blogDetail-blogPostItem']",
    # Generic Amasty Blog post wrappers — hero image + body together.
    ".amblog-post-container",
    ".amblog-post-view",
    ".amblog-index-post",
    "[data-amblog-js='post']",
    # Magento CMS wrappers.
    ".cms-page-view .column.main",
    ".cms-content",
    # Narrow fallbacks (text body only; hero image may be lost).
    ".amblog-post-content",
    "article .post-content",
    ".blog-post-content",
    # Last resort: main column. These pull in sidebar siblings on two-column
    # storefronts, which is why the strip list below has to be aggressive.
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
  const PAGE_ORIGIN = window.location.origin;
  const PAGE_HOST = window.location.hostname.replace(/^www\./i, '');

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
  //
  // But there's a subtlety: if the raw attribute is a bare Magento path such
  // as "catalog/product/9/7/image.jpg" (no leading slash, no /media/ prefix),
  // `img.src` resolves it against the current document URL — which is the
  // blog page, not the media root — producing e.g.
  //     https://www.pricerite.com.hk/hk/zh/blog-post/catalog/product/...
  // That URL 404s.  Magento serves the actual file from /media/catalog/... ,
  // so for these well-known Magento media prefixes we force-prefix /media/
  // before letting the browser resolve the URL.
  var MAGENTO_MEDIA_PREFIXES = [
    "catalog/", "wysiwyg/", "amasty/", "amblog/", "mageplaza/", "magefan_blog/"
  ];
  function magentoMediaAbs(raw) {
    if (!raw) return raw;
    var s = String(raw).trim();
    if (!s || s.startsWith("data:") || s.startsWith("blob:")
        || s.startsWith("http://") || s.startsWith("https://")
        || s.startsWith("//") || s.startsWith("/")) {
      return s;
    }
    var low = s.toLowerCase();
    for (var i = 0; i < MAGENTO_MEDIA_PREFIXES.length; i++) {
      if (low.startsWith(MAGENTO_MEDIA_PREFIXES[i])) {
        return PAGE_ORIGIN + "/media/" + s;
      }
    }
    // Not a Magento media path — let the browser resolve relative to the doc.
    return s;
  }
  function normalizeSrcset(value) {
    if (!value) return value;
    var out = [];
    var entries = value.split(",");
    for (var k = 0; k < entries.length; k++) {
      var e = entries[k].trim();
      if (!e) continue;
      var sp = e.split(/\s+/);
      var url = magentoMediaAbs(sp[0]);
      if (sp.length > 1) {
        out.push(url + " " + sp.slice(1).join(" "));
      } else {
        out.push(url);
      }
    }
    return out.join(", ");
  }

  root.querySelectorAll("img").forEach(function(liveImg) {
    // First, if the raw attribute is a Magento media path, prefix /media/
    // before the browser has a chance to resolve against the page URL.
    var rawAttr = liveImg.getAttribute("src");
    if (rawAttr) {
      var fixed = magentoMediaAbs(rawAttr);
      if (fixed !== rawAttr) liveImg.setAttribute("src", fixed);
    }
    var absSrc = liveImg.src;  // DOM property — always absolute for live elements
    if (absSrc && !absSrc.startsWith("data:") && liveImg.getAttribute("src") !== absSrc) {
      liveImg.setAttribute("src", absSrc);
    }
    // srcset on <img> — normalize every URL in the list.
    var ss = liveImg.getAttribute("srcset");
    if (ss) {
      var fixedSs = normalizeSrcset(ss);
      if (fixedSs !== ss) liveImg.setAttribute("srcset", fixedSs);
    }
  });
  // <source> elements inside <picture>/<video> — same treatment.
  root.querySelectorAll("source").forEach(function(src) {
    var s = src.getAttribute("src");
    if (s) {
      var fixed = magentoMediaAbs(s);
      if (fixed !== s) src.setAttribute("src", fixed);
      try {
        if (src.src && !src.src.startsWith("data:") && src.getAttribute("src") !== src.src) {
          src.setAttribute("src", src.src);
        }
      } catch (e) {}
    }
    var ss = src.getAttribute("srcset");
    if (ss) {
      var fixedSs = normalizeSrcset(ss);
      if (fixedSs !== ss) src.setAttribute("srcset", fixedSs);
    }
  });

  // Normalize background-image URLs in inline style= attributes.
  // The DOM property `el.style.backgroundImage` always returns an absolute
  // resolved URL (e.g. url("https://www.pricerite.com.hk/media/...")) even
  // when the raw HTML attribute had a root-relative path like url('/media/...').
  // Writing the resolved value back to the attribute ensures the captured HTML
  // contains absolute URLs so the migrated page works outside the original domain.
  root.querySelectorAll("[style]").forEach(function(el) {
    try {
      var bi = el.style.backgroundImage;
      if (bi && bi !== "none" && bi !== "") {
        var rawStyle = el.getAttribute("style") || "";
        // Replace only the url(...) tokens that are NOT already absolute HTTP.
        var fixed = rawStyle.replace(
          /url\(\s*(['"]?)(?!https?:\/\/|data:|blob:)(.*?)\1\s*\)/gi,
          function(match, quote, path) {
            if (!path) return match;
            var abs;
            try {
              abs = new URL(path, document.location.href).href;
            } catch (e) {
              abs = PAGE_ORIGIN + (path.startsWith("/") ? "" : "/") + path;
            }
            return 'url("' + abs + '")';
          }
        );
        if (fixed !== rawStyle) el.setAttribute("style", fixed);
      }
    } catch (e) { /* skip elements that throw on style access */ }
  });

  // Clone so mutations don't affect the live page before other evaluations.
  const clone = root.cloneNode(true);

  // -------- Strip chrome inside the clone (headers/footers/nav/scripts) --------
  // NOTE: iframes are NOT in the generic strip list — we preserve iframe
  // embeds for YouTube/Vimeo/etc. Unknown iframes are stripped below.
  const STRIP = [
    "script","noscript","link","meta",
    "header",".header",".page-header",".pwa-header",
    "footer",".footer",".page-footer",".pwa-footer",
    "nav",".nav",".navigation",".vertical-menu",
    // Breadcrumbs — CSS-module hashed class variants included.
    ".breadcrumbs","[class*='breadcrumbs-root']","[class*='breadcrumbs_root']",
    ".modal-popup",".modal-slide",".modals-wrapper",
    ".loading-mask",".loader",
    ".minicart-wrapper",".block-search",".search-autocomplete",
    ".cookie-notice",".cookie-consent","#cookie-status",
    ".messages",".page.messages",
    ".page-title-wrapper",
    // Generic Magento sidebar classes.
    ".sidebar",".sidebar-main",".sidebar-additional",
    // Pricerite / PWA Studio (Venia) hashed sidebar + widget wrappers.
    // Pattern is `<component>-<variant>-<hash>`, e.g. sidebar-root-BHz.
    "[class*='sidebar-root']","[class*='sidebar_root']",
    "[class*='sidebarRoot']",
    "[class*='searchBlock']","[class*='search-root']","[class*='searchRoot']",
    "[class*='favorites']","[class*='Favorites']",
    "[class*='wishlist']","[class*='Wishlist']",
    // PWA Studio blog widgets and sidebar blocks seen on Pricerite news pages.
    "[class*='categoryList-']","[class*='categoryTree-']",
    "[class*='tagList-']","[class*='tagCloud-']",
    "[class*='searchBar-']","[class*='searchForm-']",
    "[class*='newsletter-']","[class*='Newsletter-']",
    "[class*='recentPosts-']","[class*='archive-']",
    "[class*='blogSidebar-']","[class*='BlogSidebar-']",
    "[class*='postSidebar-']","[class*='PostSidebar-']",
    "[class*='shareButtons-']","[class*='socialShare-']",
    "[class*='relatedPosts-']","[class*='RelatedPosts-']",
    // Pricerite blogDetail sidebar widget boxes (分類, 搜尋, 標籤, 我的收藏清單).
    // These are siblings of blogDetail-blogPostItem inside blogDetail-blogMain.
    // Strip them so they don't appear if a broader selector is ever used.
    "[class*='blogDetail-normalBox']","[class*='blogDetail-normalHead']",
    // Amasty Blog sidebar widgets. Any of these can appear as a sibling of
    // the post when the page-main container is picked as the content root.
    ".amblog-sidebar",".amblog-widget",".amblog-widget-container",
    ".amblog-block-wrapper",".amblog-block",
    ".amblog-widget-categories",".amblog-widget-search",
    ".amblog-widget-tags",".amblog-widget-rss",
    ".amblog-widget-recent",".amblog-widget-archive",
    ".amblog-widget-featured",".amblog-widget-comment",
    ".amblog-categories-list",".amblog-tags-list",
    // Back-to-top, chat bubbles that sit at the content edge.
    ".back-to-top","[class*='backToTop']","[class*='whatsapp']",
  ];
  for (const sel of STRIP) {
    clone.querySelectorAll(sel).forEach(n => n.remove());
  }

  // -------- Preserve video embed iframes, drop everything else --------
  // Magento CMS pages embed YouTube (and sometimes Vimeo) via <iframe>.
  // We keep those so the migrated page shows the video, but we drop any
  // other iframe (chat widgets, analytics beacons, etc.) to avoid leaking
  // unrelated external content into Builder.io.
  var VIDEO_HOST_RE = /(^|\.)((?:youtube\.com)|(?:youtube-nocookie\.com)|(?:youtu\.be)|(?:vimeo\.com)|(?:player\.vimeo\.com)|(?:bilibili\.com)|(?:dailymotion\.com)|(?:wistia\.com)|(?:wistia\.net))$/i;
  function looksLikeVideoEmbed(iframe) {
    var src = iframe.getAttribute('src') || iframe.getAttribute('data-src') || '';
    if (!src) return false;
    try {
      var u = new URL(src, window.location.href);
      return VIDEO_HOST_RE.test(u.hostname.toLowerCase());
    } catch (e) { return false; }
  }
  clone.querySelectorAll('iframe').forEach(function(ifr) {
    if (looksLikeVideoEmbed(ifr)) {
      // Ensure the src resolves absolutely (was already absolute in live DOM).
      var rawSrc = ifr.getAttribute('src') || ifr.getAttribute('data-src') || '';
      if (rawSrc && /^\/\//.test(rawSrc)) {
        ifr.setAttribute('src', 'https:' + rawSrc);
      } else if (rawSrc) {
        try { ifr.setAttribute('src', new URL(rawSrc, window.location.href).toString()); }
        catch (e) {}
      }
      // Best-practice embed attributes so the video renders inside Builder.io.
      ifr.setAttribute('loading', 'lazy');
      ifr.setAttribute('allowfullscreen', '');
      if (!ifr.getAttribute('allow')) {
        ifr.setAttribute('allow',
          'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share');
      }
      if (!ifr.getAttribute('frameborder')) ifr.setAttribute('frameborder', '0');
      // Wrap in a responsive container so the iframe doesn't collapse.
      var parent = ifr.parentNode;
      if (parent && !parent.classList.contains('pr-embed-video')) {
        var wrapper = document.createElement('div');
        wrapper.className = 'pr-embed-video';
        wrapper.style.cssText =
          'position:relative; width:100%; padding-bottom:56.25%;' +
          ' height:0; overflow:hidden; margin:16px 0;';
        parent.insertBefore(wrapper, ifr);
        wrapper.appendChild(ifr);
        ifr.style.cssText =
          'position:absolute; top:0; left:0; width:100%; height:100%; border:0;';
      }
    } else {
      ifr.parentNode && ifr.parentNode.removeChild(ifr);
    }
  });
  // Remove HTML comments
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_COMMENT);
  const comments = [];
  while (walker.nextNode()) comments.push(walker.currentNode);
  comments.forEach(c => c.parentNode && c.parentNode.removeChild(c));

  // -------- Rewrite internal <a href> so they don't point at the old platform --------
  // Turns any link to the source domain into a clean root-relative path, and
  // strips the ".html" suffix so the migrated site serves extension-free URLs.
  // External links and anchors are left alone.
  function cleanInternalHref(raw) {
    if (!raw) return raw;
    var trimmed = String(raw).trim();
    if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('mailto:')
        || trimmed.startsWith('tel:') || trimmed.startsWith('javascript:')) {
      return raw;
    }
    // Resolve to an absolute URL so we can reason about origin reliably.
    var abs;
    try { abs = new URL(trimmed, window.location.href); }
    catch (e) { return raw; }

    var host = abs.hostname.replace(/^www\./i, '');
    // Only rewrite links that point to the page we're migrating from.
    if (host && host !== PAGE_HOST) return raw;

    var path = abs.pathname || '/';
    // Strip trailing .html (preserve any .html segments that are mid-path
    // by only touching the final segment).
    path = path.replace(/\.html(\/?)$/i, '$1');
    return path + (abs.search || '') + (abs.hash || '');
  }

  clone.querySelectorAll('a[href]').forEach(function(a) {
    a.setAttribute('href', cleanInternalHref(a.getAttribute('href')));
  });

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
                 || img.getAttribute("data-lazy-src")
                 || img.getAttribute("data-pb-image-url")
                 || "";
    // Prefer lazySrc when src is a data: URI placeholder.
    // NOTE: img.currentSrc is always empty on detached clone nodes — do NOT
    // rely on it. The live-element normalization above ensures rawSrc is
    // already an absolute URL for anything that was in the live DOM.
    let src = (rawSrc.startsWith("data:") && lazySrc)
      ? lazySrc
      : (rawSrc || lazySrc);
    // Last-resort fallback: scan every attribute for a URL that looks like
    // an image (any data-* attribute, srcset, etc.).  We accept both
    // media-extension paths AND bare Magento /media/ paths.
    if ((!src || src.startsWith("data:")) && img.attributes) {
      for (const attr of img.attributes) {
        const v = (attr.value || "").trim();
        if (!v || v.startsWith("data:")) continue;
        const isImageExt = /\.(jpe?g|png|gif|webp|svg)(\?|$)/i.test(v);
        const isMagentoMedia = /(\/media\/|\/catalog\/|\/wysiwyg\/|\/amasty\/)/i.test(v);
        if (isImageExt || isMagentoMedia) {
          src = v;
          break;
        }
      }
    }
    // Parse the first URL out of srcset as a final fallback.
    if ((!src || src.startsWith("data:"))) {
      const ss = img.getAttribute("srcset") || "";
      if (ss) {
        const firstUrl = ss.split(",")[0].trim().split(/\s+/)[0];
        if (firstUrl && !firstUrl.startsWith("data:")) src = firstUrl;
      }
    }
    // If we STILL don't have a real URL, drop the <img> so the migrated
    // page doesn't show a browser broken-image icon where a product
    // photo should be.
    if (!src || src.startsWith("data:")) {
      img.parentNode && img.parentNode.removeChild(img);
      return;
    }
    if (!images.includes(src)) images.push(src);
    // Ensure the clone's src attribute is the resolved absolute URL.
    if (img.getAttribute("src") !== src) img.setAttribute("src", src);
    // Normalize any srcset so Streamlit's preview iframe never receives a
    // bare Magento path (which would 404 against the localhost base URL).
    var imgSs = img.getAttribute("srcset");
    if (imgSs) {
      var fixedSs = normalizeSrcset(imgSs);
      if (fixedSs !== imgSs) img.setAttribute("srcset", fixedSs);
    }
    // Remove lazy-load attributes so Builder.io renders the image immediately
    img.removeAttribute("data-src");
    img.removeAttribute("data-lazy");
    img.removeAttribute("data-original");
    if (img.getAttribute("loading") === "lazy") img.setAttribute("loading", "eager");
  });

  // Normalize <source> tags on the clone the same way.  <picture> elements
  // on Magento product pages use <source srcset="..."> for the responsive
  // image, and if those remain as relative Magento paths the browser
  // resolves them against the wrong base URL and fails to load.
  clone.querySelectorAll("source").forEach(function(srcEl) {
    var s = srcEl.getAttribute("src");
    if (s) {
      var fixed = magentoMediaAbs(s);
      if (fixed !== s) srcEl.setAttribute("src", fixed);
    }
    var ss = srcEl.getAttribute("srcset");
    if (ss) {
      var fixedSs = normalizeSrcset(ss);
      if (fixedSs !== ss) srcEl.setAttribute("srcset", fixedSs);
    }
  });

  // -------- Meta --------
  const ogImageEl = document.querySelector('meta[property="og:image"]');
  const descEl = document.querySelector('meta[name="description"]');
  const metaTitleEl = document.querySelector('title');

  // Best-effort og:image: use the meta tag first; then fall back to the first
  // real <img> inside the captured post body (covers Pricerite's blog posts
  // where the hero image is in blogDetail-ImageBox but there is no og:image).
  let ogImage = ogImageEl ? ogImageEl.getAttribute("content") || "" : "";
  if (!ogImage && root) {
    const firstImg = root.querySelector("img");
    if (firstImg) {
      const candidateSrc = firstImg.src || firstImg.getAttribute("src") || "";
      // Skip tiny data-URI placeholders (lazy-load stubs).
      if (candidateSrc && !candidateSrc.startsWith("data:")) {
        ogImage = candidateSrc;
      }
    }
  }

  const rootFontSizePx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;

  return {
    rootSelector: rootSelector,
    html: clone.outerHTML,
    css: collectedCss.join("\n\n"),
    ruleCount: ruleCount,
    rootFontSizePx: rootFontSizePx,
    title: (document.querySelector('h1') && document.querySelector('h1').innerText.trim()) || (metaTitleEl ? metaTitleEl.innerText : ""),
    metaTitle: metaTitleEl ? metaTitleEl.innerText : "",
    metaDescription: descEl ? descEl.getAttribute("content") || "" : "",
    ogImage: ogImage,
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

            # Scroll to bottom to trigger lazy-loaded images and AJAX-loaded
            # carousels (e.g. "Related Products" blocks that only fetch their
            # image data once the element scrolls into view).  Two full passes
            # with longer dwell times so below-the-fold Slick carousels have
            # a chance to fetch their tile images before we capture.
            try:
                await page.evaluate(
                    "async () => {"
                    "  const step = 320;"
                    "  const h = () => document.body.scrollHeight;"
                    "  for (let pass = 0; pass < 2; pass++) {"
                    "    let y = 0;"
                    "    while (y < h()) {"
                    "      window.scrollTo(0, y);"
                    "      await new Promise(r => setTimeout(r, 150));"
                    "      y += step;"
                    "    }"
                    "    window.scrollTo(0, h());"
                    "    await new Promise(r => setTimeout(r, 800));"
                    "  }"
                    # Explicitly scroll every Slick slider / product grid into
                    # view and sit there for a moment so Magento's own
                    # intersection-observer lazy-load and AJAX calls fire.
                    "  const targets = document.querySelectorAll("
                    "    '.slick-slider, [data-content-type=\"products\"],"
                    "     .products-list, .block-products-list, .products-grid,"
                    "     .product-item, .product-items'"
                    "  );"
                    "  for (const el of targets) {"
                    "    try { el.scrollIntoView({block:'center'}); } catch(e) {}"
                    "    await new Promise(r => setTimeout(r, 400));"
                    "  }"
                    "  window.scrollTo(0, 0);"
                    "  await new Promise(r => setTimeout(r, 300));"
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
            #
            # slickGoTo(0) alone doesn't hydrate pages 2+ of a carousel — Slick
            # only populates data-lazy for the slide range it's about to show.
            # We step through every slide index on every slider so every
            # product image in the track gets a real URL.
            try:
                await page.evaluate(
                    "async () => {"
                    "  var ORIGIN = window.location.origin;"
                    "  var MAGENTO_PREFIXES = ["
                    "    'catalog/','wysiwyg/','amasty/','amblog/','mageplaza/','magefan_blog/'"
                    "  ];"
                    # Bare Magento paths like 'catalog/product/9/7/img.jpg' must
                    # be resolved as ORIGIN + '/media/' + path, not against the
                    # current document URL (which would yield a 404).
                    "  function magentoAbs(raw) {"
                    "    if (!raw) return raw;"
                    "    var t = String(raw).trim();"
                    "    if (!t || t.startsWith('data:') || t.startsWith('blob:')"
                    "        || t.startsWith('http://') || t.startsWith('https://')"
                    "        || t.startsWith('//') || t.startsWith('/')) return t;"
                    "    var low = t.toLowerCase();"
                    "    for (var p = 0; p < MAGENTO_PREFIXES.length; p++) {"
                    "      if (low.startsWith(MAGENTO_PREFIXES[p])) {"
                    "        return ORIGIN + '/media/' + t;"
                    "      }"
                    "    }"
                    "    return t;"
                    "  }"
                    "  function pullLazy(img) {"
                    "    var lazy = img.getAttribute('data-src')"
                    "            || img.getAttribute('data-lazy')"
                    "            || img.getAttribute('data-original')"
                    "            || img.getAttribute('data-srcset');"
                    "    if (!lazy) {"
                    "      var ss = img.getAttribute('srcset') || '';"
                    "      if (ss) lazy = ss.split(',')[0].trim().split(/\\s+/)[0];"
                    "    }"
                    "    var s = img.getAttribute('src') || '';"
                    "    if (lazy && (s.startsWith('data:') || !s)) {"
                    "      img.src = magentoAbs(lazy);"
                    "    } else if (s && !s.startsWith('data:')) {"
                    # Catch images whose original src was already a bare
                    # Magento path that the page loaded incorrectly.
                    "      var fixed = magentoAbs(s);"
                    "      if (fixed !== s) img.src = fixed;"
                    "    }"
                    "  }"
                    ""
                    "  function findJQuery() {"
                    "    if (window.jQuery && window.jQuery.fn && window.jQuery.fn.slick) return window.jQuery;"
                    "    if (window.$ && window.$.fn && window.$.fn.slick) return window.$;"
                    "    if (window.require) {"
                    "      try {"
                    "        var j = window.require('jquery');"
                    "        if (j && j.fn && j.fn.slick) return j;"
                    "      } catch (e) {}"
                    "    }"
                    "    return null;"
                    "  }"
                    ""
                    "  document.querySelectorAll('img').forEach(pullLazy);"
                    ""
                    "  var $$ = findJQuery();"
                    "  if ($$) {"
                    "    var sliders = document.querySelectorAll('.slick-slider, .slick-initialized');"
                    "    for (var i = 0; i < sliders.length; i++) {"
                    "      try {"
                    "        var $s = $$(sliders[i]);"
                    "        var slides = sliders[i].querySelectorAll('.slick-slide:not(.slick-cloned)');"
                    "        for (var j = 0; j < slides.length; j++) {"
                    "          try { $s.slick('slickGoTo', j, true); } catch (e) {}"
                    "          await new Promise(function(r){ setTimeout(r, 120); });"
                    "          sliders[i].querySelectorAll('img').forEach(pullLazy);"
                    "        }"
                    "        try { $s.slick('slickGoTo', 0, true); } catch (e) {}"
                    "      } catch (e) {}"
                    "    }"
                    "  }"
                    ""
                    # One more pass: resolve any <img> whose src still begins
                    # with data:, by walking every data-* attribute for a URL.
                    "  document.querySelectorAll('img').forEach(function(img) {"
                    "    var s = img.getAttribute('src') || '';"
                    "    if (!s.startsWith('data:') && s) return;"
                    "    for (var a = 0; a < img.attributes.length; a++) {"
                    "      var attr = img.attributes[a];"
                    "      var v = (attr.value || '').trim();"
                    "      if (!v || v.startsWith('data:')) continue;"
                    "      if (/\\.(jpe?g|png|gif|webp|svg)(\\?|$)/i.test(v)) {"
                    "        img.src = magentoAbs(v);"
                    "        break;"
                    "      }"
                    "    }"
                    "  });"
                    # <source> elements inside <picture>: if their srcset or
                    # src is a bare Magento path the browser never fetches
                    # the right file.  Rewrite to /media/-absolute URL here.
                    "  document.querySelectorAll('source').forEach(function(src) {"
                    "    var s = src.getAttribute('src');"
                    "    if (s) { var f = magentoAbs(s); if (f !== s) src.setAttribute('src', f); }"
                    "    var ss = src.getAttribute('srcset');"
                    "    if (ss) {"
                    "      var parts = ss.split(',').map(function(p){"
                    "        p = p.trim(); if (!p) return '';"
                    "        var sp = p.split(/\\s+/);"
                    "        var u = magentoAbs(sp[0]);"
                    "        return sp.length > 1 ? (u + ' ' + sp.slice(1).join(' ')) : u;"
                    "      }).filter(Boolean).join(', ');"
                    "      if (parts !== ss) src.setAttribute('srcset', parts);"
                    "    }"
                    "  });"
                    ""
                    "  document.querySelectorAll('img').forEach(pullLazy);"
                    "}"
                )
                # Give the browser time to actually fetch & decode the newly-
                # set srcs. Slick can take a while to settle after stepping
                # through every slide.
                await page.wait_for_timeout(3000)

                # Wait for every <img> with a real src to finish loading so
                # the clone has complete dimensions. Any image still pending
                # after 10s is left as-is — better than blocking the whole run.
                try:
                    await page.evaluate(
                        "() => Promise.race(["
                        "  Promise.all(Array.from(document.images).map(function(img){"
                        "    if (img.complete && img.naturalHeight !== 0) return Promise.resolve();"
                        "    return new Promise(function(res){"
                        "      img.addEventListener('load', res, {once:true});"
                        "      img.addEventListener('error', res, {once:true});"
                        "    });"
                        "  })),"
                        "  new Promise(function(r){ setTimeout(r, 10000); })"
                        "])"
                    )
                except Exception:
                    pass

                # Fire resize so any JS-driven layout (Slick, Swiper, etc.)
                # recalculates widths against the current viewport.
                try:
                    await page.evaluate(
                        "() => window.dispatchEvent(new Event('resize'))"
                    )
                    await page.wait_for_timeout(400)
                except Exception:
                    pass
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
            root_font_size_px = float(payload.get("rootFontSizePx") or 16)

            # Strip dangerous positioning that escapes Builder.io's Custom Code
            # container. Sticky/fixed headers inside a block float over
            # unrelated parts of the Builder page.
            css = re.sub(
                r"position\s*:\s*(sticky|fixed)\s*;?",
                "",
                css,
                flags=re.IGNORECASE,
            )

            # Convert rem values in CSS rule bodies to absolute px so that font
            # sizes render identically inside Builder.io (where the html element
            # has 16 px) as on the original Magento/PWA-Studio site (where the
            # theme typically sets html { font-size: 62.5% } = 10 px).
            css = _convert_rem_to_px(css, root_font_size_px)

            # Replace Magento product carousels with self-contained static
            # tiles.  Magento fills price/title/cart via Knockout bindings,
            # which get stripped when <script> tags are removed for Builder.
            # Rebuild from the DOM data we captured so tiles render fully.
            try:
                from .product_tile_builder import rebuild_product_tiles
                html = rebuild_product_tiles(html)
            except Exception as e:
                logger.warning("product_tile_builder failed (non-fatal): %s", e)

            # Final assembly: one self-contained fragment.
            # Append carousel CSS fixes to the captured stylesheet so they
            # override any conflicting Slick rules already in `css`.
            # The carousel reinit script re-wires arrow buttons and converts
            # Slick's pixel-based layout to percentage-based so it works at
            # any viewport width inside Builder.io.
            # If og:image wasn't populated, fall back to the largest <img>
            # we captured — this is the cover image for news/blog posts that
            # don't expose an Open Graph tag. `images` is ordered by DOM
            # position, and lazy/srcset resolution already ran above, so
            # images[0] is almost always the hero banner.
            if not result.og_image and result.images:
                result.og_image = result.images[0]

            # Guarantee the cover image appears in the rendered fragment.
            # Builder.io's blog-post model renders `html_content` as the
            # article body — the `thumbnail` field is metadata only, not
            # auto-inserted into the body. So we prepend the hero <img>
            # here if it isn't already present in the captured HTML.
            hero_html = ""
            if result.og_image and result.og_image not in html:
                _esc_alt = (result.title or "").replace('"', '&quot;')
                hero_html = (
                    '<div class="migrated-hero-image" '
                    'style="margin:0 0 24px 0;">'
                    f'<img src="{result.og_image}" alt="{_esc_alt}" '
                    'style="width:100%;height:auto;display:block;" />'
                    "</div>\n"
                )

            result.html_fragment = (
                '<div class="migrated-live-content">\n'
                f"<style>\n{css}\n{_CAROUSEL_CSS_FIXES}\n</style>\n"
                f"<script>\n{_CAROUSEL_REINIT_JS}\n</script>\n"
                f"<script>\n{_TAB_REINIT_JS}\n</script>\n"
                f"{hero_html}"
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
