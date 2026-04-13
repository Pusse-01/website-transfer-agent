"""
Web page scraper module for Magento websites.
Supports:
- Blog posts via Amasty Blog GraphQL API + HTML fallback
- Static CMS pages via Magento cmsPage GraphQL + HTML fallback
- Playwright-based full-page rendering (primary, when available)
"""

import json
import re
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Playwright dependency
# ---------------------------------------------------------------------------
PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    pass

# ---------------------------------------------------------------------------
# JavaScript to run inside Playwright: extracts the rendered content area
# with computed layout styles applied as inline attributes so the output
# is visually faithful without needing any external CSS file.
# ---------------------------------------------------------------------------
_PLAYWRIGHT_EXTRACTION_JS = r"""
async () => {
    /* === 1. Find content area FIRST (before we remove anything) === */
    const CANDIDATES = [
        '.amblog-post-content',
        '.cms-content',
        '.post-content',
        '.blog-post-content',
        'article',
        '.page-main-content',
        '.page-main',
        'main',
    ];
    let container = null;
    for (const sel of CANDIDATES) {
        try {
            const el = document.querySelector(sel);
            if (el && el.innerText.trim().length > 100) { container = el; break; }
        } catch(e) {}
    }
    if (!container) return null;

    /* === 1.4 AGGRESSIVELY remove hover-overlay action menus.
       These are the dark floating buttons ("加入收藏清單" / "加入產品比較" /
       "找相似" / "加到購物車") that overlay the product image on hover.
       Without JS + hover, they show up stuck-open and cover the products.
       We physically REMOVE them — users can still click through to the
       product detail page from the product-item link. */

    /* Simplest approach: remove the entire .product-item-inner block.
       This contains ALL action buttons (add-to-cart, wishlist, compare,
       out-of-stock) regardless of the exact class names used by the theme.
       The product name and price (in .product-item-details above .product-item-inner)
       remain untouched. */
    try {
        container.querySelectorAll(
            '[class*="product-item"] .product-item-inner, ' +
            '[class*="product-item"] .product-item-actions, ' +
            '[class*="product-item"] .actions-primary, ' +
            '[class*="product-item"] .actions-secondary, ' +
            '[class*="product-item"] form'
        ).forEach(el => el.remove());
    } catch(e) {}

    /* Also remove any remaining action/cart/wishlist/compare links & buttons
       that live outside .product-item-inner (some themes render them differently) */
    const HOVER_OVERLAY_SELECTORS = [
        '.product-item .tocart',
        '.product-item .towishlist',
        '.product-item .tocompare',
        '.product-item [class*="tocart"]',
        '.product-item [class*="towishlist"]',
        '.product-item [class*="tocompare"]',
        '.product-item [class*="find-similar"]',
        '.product-item [class*="findsimilar"]',
        '.product-item [class*="amsearch"]',
        '.product-item [class*="quickview"]',
        '.product-item .product-image-actions',
        '.product-item .hover-actions',
        '.product-item .hover-overlay',
        '.product-item .products-list-details-box',
        /* Slick nav (renders as text without CSS) */
        '.slick-prev', '.slick-next', '.slick-arrow', '.slick-dots',
    ];
    HOVER_OVERLAY_SELECTORS.forEach(sel => {
        try { container.querySelectorAll(sel).forEach(el => el.remove()); } catch(e) {}
    });

    /* === 1.5 Capture ALL relevant CSS rules BEFORE stripping chrome ===
       We walk every rule in every accessible stylesheet and KEEP only the rules
       whose selector actually matches an element in our container.  For CORS-
       blocked stylesheets (href only, cssRules throws) we fetch the href as
       plain text and parse it with a fresh CSSStyleSheet.
    */
    const collectedCSS = [];
    const collectedFontFaces = [];
    const collectedKeyframes = new Map();

    function selectorMatchesContainer(selector) {
        /* Strip pseudo-classes/elements that break querySelectorAll */
        const cleaned = selector
            .replace(/::?(?:hover|focus|active|visited|before|after|placeholder|selection|first-line|first-letter|-[a-z-]+)(?:\([^)]*\))?/gi, '')
            .replace(/:(?:not|is|where|has)\([^)]*\)/gi, '')
            .trim();
        if (!cleaned) return true;  /* keep — e.g. universal or complex selector */
        try {
            /* Match if ANY part of the selector list matches */
            const parts = cleaned.split(',').map(s => s.trim()).filter(Boolean);
            for (const p of parts) {
                try {
                    if (container.matches(p)) return true;
                    if (container.querySelector(p)) return true;
                } catch(e) { /* invalid selector — keep to be safe */ return true; }
            }
            return false;
        } catch(e) { return true; }
    }

    function processRules(rules) {
        if (!rules) return;
        for (const rule of Array.from(rules)) {
            try {
                /* CSSStyleRule */
                if (rule.type === 1 && rule.selectorText) {
                    if (selectorMatchesContainer(rule.selectorText)) {
                        collectedCSS.push(rule.cssText);
                    }
                }
                /* CSSMediaRule — recurse */
                else if (rule.type === 4 && rule.cssRules) {
                    const inner = [];
                    for (const r of Array.from(rule.cssRules)) {
                        if (r.type === 1 && r.selectorText && selectorMatchesContainer(r.selectorText)) {
                            inner.push(r.cssText);
                        }
                    }
                    if (inner.length) {
                        collectedCSS.push('@media ' + rule.conditionText + ' {\n' + inner.join('\n') + '\n}');
                    }
                }
                /* CSSSupportsRule — recurse */
                else if (rule.type === 12 && rule.cssRules) {
                    const inner = [];
                    for (const r of Array.from(rule.cssRules)) {
                        if (r.type === 1 && r.selectorText && selectorMatchesContainer(r.selectorText)) {
                            inner.push(r.cssText);
                        }
                    }
                    if (inner.length) {
                        collectedCSS.push('@supports ' + rule.conditionText + ' {\n' + inner.join('\n') + '\n}');
                    }
                }
                /* CSSKeyframesRule — keep them all (animations referenced by animation-name) */
                else if (rule.type === 7) {
                    collectedKeyframes.set(rule.name, rule.cssText);
                }
                /* CSSFontFaceRule */
                else if (rule.type === 5) {
                    collectedFontFaces.push(rule.cssText);
                }
            } catch(e) { /* skip bad rule */ }
        }
    }

    /* For each stylesheet: try cssRules first; on CORS failure, fetch the href
       as text and parse with a new CSSStyleSheet (works in all Chromium). */
    const corsFetchPromises = [];
    for (const sheet of Array.from(document.styleSheets)) {
        let got = false;
        try {
            if (sheet.cssRules) { processRules(sheet.cssRules); got = true; }
        } catch(e) { /* CORS */ }
        if (!got && sheet.href) {
            const href = sheet.href;
            corsFetchPromises.push(
                fetch(href, { credentials: 'same-origin' })
                    .then(r => r.ok ? r.text() : '')
                    .then(cssText => {
                        if (!cssText) return;
                        try {
                            const s = new CSSStyleSheet();
                            s.replaceSync(cssText);
                            processRules(s.cssRules);
                        } catch(e) {
                            /* CSSStyleSheet.replaceSync may not be available — fall
                               back to raw text inclusion so the rules still ship. */
                            collectedCSS.push('/* cors-fetched: ' + href + ' */\n' + cssText);
                        }
                    })
                    .catch(() => { /* network error — skip */ })
            );
        }
    }
    await Promise.all(corsFetchPromises);

    /* Also include inline <style> tags verbatim (the original-site authors may
       have put !important overrides there that we want to ship). */
    document.querySelectorAll('style').forEach(s => {
        const txt = s.textContent || '';
        if (txt && txt.length < 500000) collectedCSS.push('/* inline <style> */\n' + txt);
    });

    const capturedCSS =
        Array.from(collectedKeyframes.values()).join('\n') + '\n' +
        collectedFontFaces.join('\n') + '\n' +
        collectedCSS.join('\n');

    /* === 2. Strip chrome/navigation from DOM === */
    const REMOVE = [
        'script', 'noscript',
        'header', '.header', '.page-header', '.pwa-header',
        'footer', '.footer', '.page-footer', '.pwa-footer',
        'nav', '.navigation', '.nav',
        '.breadcrumbs',
        '.minicart-wrapper', '.block-search',
        '.modal-popup', '.modal-slide', '.modals-wrapper',
        '.loading-mask', '.loader',
        '.cookie-notice', '.cookie-consent',
        '#cookie-status',
        '.page-title-wrapper',
        '.sidebar', '.sidebar-main',
        /* Slick navigation — these become raw text "Previous"/"Next" without CSS */
        '.slick-prev', '.slick-next', '.slick-arrow', '.slick-dots',
    ];
    REMOVE.forEach(sel => {
        try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch(e) {}
    });

    /* === 2.3 Fix Slick carousels — simpler "force-visible" approach ===
       Un-Slick DOM reconstruction is fragile because Magento themes vary
       in whether products are in <li>, <div>, etc.  Instead we:
         a) remove clone slides (exact duplicates, not needed)
         b) remove Slick nav buttons (text-only without CSS)
         c) reset the track transform and give every real slide
            position:static + visibility:visible + opacity:1 inline
       Python will add CSS to make .slick-list scroll horizontally.
    */
    try {
        container.querySelectorAll('.slick-initialized, .slick-slider').forEach(slickEl => {
            /* Record how many items were active (= carousel column count) */
            const activeCount = slickEl.querySelectorAll(
                '.slick-active:not(.slick-cloned)'
            ).length;
            if (activeCount > 0) {
                slickEl.setAttribute('data-pc-carousel-count', String(activeCount));
            }

            /* Remove clone slides — they're duplicates we don't need */
            slickEl.querySelectorAll('.slick-cloned').forEach(el => el.remove());

            /* Remove text-only nav that looks broken without CSS */
            slickEl.querySelectorAll(
                '.slick-prev, .slick-next, .slick-arrow, .slick-dots'
            ).forEach(el => el.remove());

            /* Reset the track so it doesn't translate off-screen */
            const track = slickEl.querySelector('.slick-track');
            if (track) {
                track.style.setProperty('transform', 'none', 'important');
                track.style.setProperty('width', 'auto', 'important');
                track.style.setProperty('transition', 'none', 'important');
            }

            /* Force ALL slides to be visible */
            slickEl.querySelectorAll('.slick-slide').forEach(slide => {
                slide.style.setProperty('visibility', 'visible', 'important');
                slide.style.setProperty('opacity', '1', 'important');
                slide.style.setProperty('display', 'block', 'important');
                slide.style.removeProperty('width');  /* let flex/CSS set width */
            });

            /* Allow the list to scroll horizontally */
            const list = slickEl.querySelector('.slick-list');
            if (list) {
                list.style.setProperty('overflow-x', 'auto', 'important');
                list.style.setProperty('overflow-y', 'visible', 'important');
                list.style.setProperty('height', 'auto', 'important');
            }
        });
    } catch(e) {}

    /* === 2.4 Reset overflow on Page Builder wrappers === */
    [
        '[data-content-type="slider"]',
        '[data-content-type="products"]',
        '.widget.block-products-list',
    ].forEach(sel => {
        try {
            container.querySelectorAll(sel).forEach(el => {
                el.style.setProperty('overflow', 'visible', 'important');
            });
        } catch(e) {}
    });

    /* === 3. Apply computed layout styles as inline attributes ===
       We capture only properties that are:
       (a) layout-critical (flex, grid, background, border)
       (b) NOT the browser default (skip block, inline, transparent, etc.)
       Width values are intentionally excluded because computed pixel widths
       break responsiveness — the Page Builder inline styles already set them
       as percentages via data-pb-style.
    */
    const LAYOUT_PROPS = [
        'display',
        'flex-direction', 'flex-wrap', 'flex-grow', 'flex-shrink', 'flex-basis',
        'align-items', 'justify-content', 'align-self', 'align-content',
        'grid-template-columns', 'grid-template-rows',
        'gap', 'column-gap', 'row-gap',
        'box-sizing', 'float', 'clear',
        'overflow', 'overflow-x', 'overflow-y',
        'background-color', 'background-image',
        'border-radius',
        'color', 'font-size', 'font-weight',
        'text-align',
    ];

    const SKIP_VALUES = new Set([
        '', 'none', 'initial', 'normal', 'auto',
        'rgba(0, 0, 0, 0)', 'transparent',
        'visible', 'content-box', 'static',
        '0px', '0px 0px', '0px 0px 0px', '0px 0px 0px 0px',
        'rgb(255, 255, 255)',   // white background (default page bg)
        'rgb(0, 0, 0)',         // black text (default)
        '16px',                  // default font-size
    ]);
    const SKIP_DISPLAY = new Set(['block', 'inline', 'table-row', 'table-cell',
                                   'table', 'table-row-group', 'list-item']);

    function applyLayoutStyles(el, depth) {
        if (depth > 30 || !el || !el.tagName) return;
        if (['SCRIPT','STYLE','LINK','META','HEAD','NOSCRIPT'].includes(el.tagName)) return;

        const cs = window.getComputedStyle(el);
        const existing = el.getAttribute('style') || '';
        const decls = [];

        for (const prop of LAYOUT_PROPS) {
            if (existing.includes(prop + ':')) continue;  // already set inline
            const val = cs.getPropertyValue(prop);
            if (!val || SKIP_VALUES.has(val)) continue;
            if (prop === 'display' && SKIP_DISPLAY.has(val)) continue;
            if (prop === 'background-image' && val === 'none') continue;
            // Don't capture overflow:hidden from carousel wrappers — Python will set scroll-snap
            if ((prop === 'overflow' || prop === 'overflow-x') && val === 'hidden') continue;
            decls.push(prop + ': ' + val);
        }

        if (decls.length) {
            el.setAttribute('style',
                (existing ? existing.replace(/;\s*$/, '') + '; ' : '') +
                decls.join('; ') + ';'
            );
        }
        Array.from(el.children).forEach(c => applyLayoutStyles(c, depth + 1));
    }

    applyLayoutStyles(container, 0);

    /* === 4. Make all URLs absolute === */
    container.querySelectorAll('img').forEach(img => {
        try { img.setAttribute('src', img.src); } catch(e) {}
        // also handle srcset
        const ss = img.getAttribute('srcset');
        if (ss) {
            const absSrcset = ss.replace(/(\S+)(\s+\S+)?/g, (m, url, descr) => {
                try { return new URL(url, location.href).href + (descr || ''); } catch(e) { return m; }
            });
            img.setAttribute('srcset', absSrcset);
        }
    });
    container.querySelectorAll('[data-background-images]').forEach(el => {
        // Make Magento background-image JSON URLs absolute (already handled by CSS processor)
    });
    container.querySelectorAll('a[href]').forEach(a => {
        try {
            const href = a.getAttribute('href');
            if (href && !href.startsWith('javascript') && !href.startsWith('mailto') && !href.startsWith('#')) {
                a.setAttribute('href', a.href);
            }
        } catch(e) {}
    });

    /* === 5. Collect metadata === */
    const metaDesc = document.querySelector('meta[name="description"]');
    const ogImage  = document.querySelector('meta[property="og:image"]');
    const h1       = document.querySelector('h1');

    /* Prepend the captured CSS as a <style> block so it travels with the HTML.
       This is the key insight: the original live site has the styling we want,
       and document.styleSheets gives us every rule.  We filter to the ones that
       actually match the container so we don't ship 500KB of unused CSS. */
    const finalHTML = (capturedCSS.trim()
        ? '<style data-source="original-site">\n' + capturedCSS + '\n</style>\n'
        : '') + container.outerHTML;

    return {
        html: finalHTML,
        captured_css_bytes: capturedCSS.length,
        title: document.title,
        h1_title: h1 ? h1.innerText.trim() : '',
        description: metaDesc ? metaDesc.getAttribute('content') : '',
        thumbnail: ogImage   ? ogImage.getAttribute('content')   : '',
    };
}
"""


def _run_playwright_scrape(url: str, base_url: str = "") -> dict | None:
    """
    Open *url* in headless Chromium, wait for full JS rendering, then extract
    the content area with computed layout styles applied as inline attributes.

    Returns a page_data dict compatible with BlogScraper / StaticPageScraper
    return values, or None if Playwright is unavailable or the scrape fails.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None

    logger.info("Playwright scraping: %s", url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-HK",
                extra_http_headers={"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"},
            )
            page = ctx.new_page()

            # Hide webdriver flag (basic anti-bot bypass)
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            page.goto(url, wait_until="networkidle", timeout=60_000)
            # Extra wait for lazy-loaded content (carousels, widgets)
            page.wait_for_timeout(4000)

            # Dismiss cookie consent if present
            for dismiss_sel in [
                ".cookie-notice .action-dismiss",
                ".cookie-consent button",
                "#cookie-accept",
                ".accept-cookies",
            ]:
                try:
                    page.click(dismiss_sel, timeout=1000)
                    page.wait_for_timeout(500)
                    break
                except Exception:
                    pass

            # Scroll down to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            result = page.evaluate(_PLAYWRIGHT_EXTRACTION_JS)
            browser.close()

        if not result or not result.get("html"):
            logger.warning("Playwright returned empty content for %s", url)
            return None

        css_bytes = result.get("captured_css_bytes", 0)
        logger.info(
            "Playwright scrape: %d bytes HTML, %d bytes captured CSS from original site",
            len(result.get("html", "")), css_bytes
        )

        url_key = url.rstrip("/").split("/")[-1]
        return {
            "title": result.get("h1_title") or result.get("title", ""),
            "html_content": result["html"],
            "thumbnail": result.get("thumbnail", ""),
            "meta_description": result.get("description", ""),
            "meta_title": result.get("title", ""),
            "categories": [],
            "tags": [],
            "url_key": url_key,
            "published_at": "",
            "images": [],
            "source": "playwright",
            "page_type": "blog",
        }

    except Exception as exc:
        logger.warning("Playwright scrape failed for %s: %s", url, exc)
        return None


class BlogScraper:
    """Scrapes blog content from a Magento website with Amasty Blog."""

    def __init__(self, base_url: str, blog_path: str = "/blog/"):
        self.base_url = base_url.rstrip("/")
        self.blog_path = blog_path
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; BlogMigrationAgent/1.0)",
            "Accept": "text/html,application/json",
        })
        self.graphql_url = f"{self.base_url}/graphql"

    def fetch_post_by_url_key(self, url_key: str, use_playwright: bool = True) -> dict:
        """Fetch a single blog post by its URL key.

        Priority:
        1. Playwright (full rendering with computed styles) — highest fidelity
        2. GraphQL (structured data) — fast but loses Page Builder CSS
        3. HTML scraping — last resort
        """
        logger.info(f"Fetching blog post: {url_key}")
        full_url = f"{self.base_url}{self.blog_path}{url_key}"

        # 1. Try Playwright (gets computed styles — best layout fidelity)
        if use_playwright and PLAYWRIGHT_AVAILABLE:
            pw_result = _run_playwright_scrape(full_url, base_url=self.base_url)
            if pw_result and pw_result.get("html_content"):
                pw_result["page_type"] = "blog"
                pw_result["url_key"] = url_key
                logger.info("Playwright scrape succeeded for %s", url_key)
                return pw_result

        # 2. Try GraphQL
        post = self._fetch_via_graphql(url_key)
        if post:
            return post

        # 3. Fallback to HTML scraping
        logger.info(f"GraphQL failed for {url_key}, falling back to HTML scraping")
        return self._fetch_via_html(url_key)

    def fetch_all_posts_via_graphql(self, page: int = 1) -> list[dict]:
        """Fetch paginated blog post listing via GraphQL."""
        query = """
        query GetBlogPosts($page: Int!) {
            amBlogPosts(type: ALL, page: $page) {
                all_post_size
                items {
                    post_id
                    title
                    url_key
                    short_content
                    post_thumbnail
                    list_thumbnail
                    published_at
                    categories
                    tags {
                        name
                        url_key
                        tag_id
                    }
                    tag_ids
                }
            }
        }
        """
        try:
            response = self.session.post(
                self.graphql_url,
                json={"query": query, "variables": {"page": page}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.warning(f"GraphQL listing errors: {data['errors']}")
                return []

            posts_data = data.get("data", {}).get("amBlogPosts", {})
            total = posts_data.get("all_post_size", 0)
            items = posts_data.get("items", [])
            logger.info(f"Fetched page {page}: {len(items)} posts (total: {total})")
            return items

        except Exception as e:
            logger.warning(f"GraphQL listing failed: {e}")
            return []

    def _fetch_via_graphql(self, url_key: str) -> dict | None:
        """Fetch blog post via Magento GraphQL API (Amasty Blog).

        Uses a progressive query strategy: starts with all fields, then
        retries with fewer fields if the API rejects unknown ones.
        """
        queries = [
            # Attempt 1: full fields with tags as object type (AmBlogTags)
            """
            query GetBlogPost($urlKey: String!) {
                amBlogPost(urlKey: $urlKey) {
                    post_id
                    title
                    full_content
                    short_content
                    post_thumbnail
                    post_thumbnail_alt
                    list_thumbnail
                    list_thumbnail_alt
                    meta_title
                    meta_description
                    meta_tags
                    categories
                    tags {
                        name
                        url_key
                        tag_id
                    }
                    tag_ids
                    url_key
                    published_at
                    created_at
                    updated_at
                    status
                    author_id
                    views
                    is_featured
                }
            }
            """,
            # Attempt 2: minimal safe fields (no tags/categories)
            """
            query GetBlogPost($urlKey: String!) {
                amBlogPost(urlKey: $urlKey) {
                    post_id
                    title
                    full_content
                    short_content
                    post_thumbnail
                    list_thumbnail
                    meta_title
                    meta_description
                    url_key
                    published_at
                    status
                }
            }
            """,
        ]

        for i, query in enumerate(queries):
            try:
                response = self.session.post(
                    self.graphql_url,
                    json={"query": query, "variables": {"urlKey": url_key}},
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()

                if "errors" in data:
                    logger.warning(f"GraphQL attempt {i+1} failed for {url_key}: {data['errors']}")
                    continue

                post_data = data.get("data", {}).get("amBlogPost")
                if not post_data:
                    return None

                return self._normalize_graphql_post(post_data)

            except Exception as e:
                logger.warning(f"GraphQL request failed for {url_key}: {e}")
                return None

        return None

    def _normalize_graphql_post(self, post_data: dict) -> dict:
        """Normalize GraphQL response into a standard format."""
        thumbnail = post_data.get("post_thumbnail") or post_data.get("list_thumbnail") or ""
        if thumbnail and not thumbnail.startswith("http"):
            thumbnail = urljoin(self.base_url, thumbnail)

        html_content = post_data.get("full_content") or post_data.get("short_content") or ""

        raw_tags = post_data.get("tags") or []
        if isinstance(raw_tags, str):
            tags = [raw_tags] if raw_tags else []
        elif isinstance(raw_tags, list) and raw_tags:
            if isinstance(raw_tags[0], dict):
                tags = [t.get("name", "") for t in raw_tags if t.get("name")]
            else:
                tags = [str(t) for t in raw_tags if t]
        else:
            tags = []

        return {
            "title": post_data.get("title", ""),
            "html_content": html_content,
            "thumbnail": thumbnail,
            "thumbnail_alt": post_data.get("post_thumbnail_alt", ""),
            "meta_title": post_data.get("meta_title", ""),
            "meta_description": post_data.get("meta_description", ""),
            "categories": post_data.get("categories", []),
            "tags": tags,
            "url_key": post_data.get("url_key", ""),
            "published_at": post_data.get("published_at", ""),
            "created_at": post_data.get("created_at", ""),
            "updated_at": post_data.get("updated_at", ""),
            "images": self._extract_images_from_html(html_content),
            "source": "graphql",
            "page_type": "blog",
        }

    def _fetch_via_html(self, url_key: str) -> dict:
        """Fetch blog post by scraping the HTML page."""
        url = f"{self.base_url}{self.blog_path}{url_key}"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch HTML for {url_key}: {e}")
            return {"error": str(e), "url_key": url_key}

        soup = BeautifulSoup(response.text, "html.parser")
        result = self._parse_html_post(soup, url_key, url)
        result["page_type"] = "blog"
        return result

    def _parse_html_post(self, soup: BeautifulSoup, url_key: str, url: str) -> dict:
        """Parse blog post from HTML page."""
        title = ""
        for selector in ["h1.page-title", "h1.post-title", "h1.amblog-title", ".amblog-post-title", "h1"]:
            el = soup.select_one(selector)
            if el:
                title = el.get_text(strip=True)
                break

        html_content = ""
        for selector in [
            ".amblog-post-content",
            ".amblog-content",
            ".post-content",
            ".blog-post-content",
            "article .content",
            ".entry-content",
            "article",
        ]:
            el = soup.select_one(selector)
            if el:
                # Remove non-content elements
                for unwanted in el.select("script, noscript, iframe, link, meta, nav, .nav, header, footer"):
                    unwanted.decompose()
                html_content = str(el)
                break

        if not html_content:
            main = soup.select_one("main") or soup.select_one("#maincontent")
            if main:
                for unwanted in main.select("script, noscript, iframe, link, meta, nav, .nav, header, footer"):
                    unwanted.decompose()
                html_content = str(main)

        thumbnail = ""
        for selector in [
            ".amblog-post-image img",
            ".post-thumbnail img",
            'meta[property="og:image"]',
            ".amblog-element-post img",
        ]:
            el = soup.select_one(selector)
            if el:
                thumbnail = el.get("src") or el.get("content", "")
                if thumbnail and not thumbnail.startswith("http"):
                    thumbnail = urljoin(url, thumbnail)
                break

        meta_desc = ""
        meta_el = soup.select_one('meta[name="description"]')
        if meta_el:
            meta_desc = meta_el.get("content", "")

        meta_title = ""
        if soup.title:
            meta_title = soup.title.string or ""

        return {
            "title": title,
            "html_content": html_content,
            "thumbnail": thumbnail,
            "meta_title": meta_title,
            "meta_description": meta_desc,
            "categories": [],
            "tags": [],
            "url_key": url_key,
            "published_at": "",
            "images": self._extract_images_from_html(html_content),
            "source": "html",
        }

    def _extract_images_from_html(self, html_content: str) -> list[str]:
        """Extract all image URLs from HTML content."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "html.parser")
        images = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src:
                if not src.startswith("http"):
                    src = urljoin(self.base_url, src)
                images.append(src)

        return list(dict.fromkeys(images))

    def fetch_blog_list_from_sitemap(self) -> list[str]:
        """Try to get blog URL keys from sitemap."""
        sitemap_urls = [
            f"{self.base_url}/sitemap.xml",
            f"{self.base_url}/pub/sitemap/sitemap.xml",
        ]
        url_keys = []

        for sitemap_url in sitemap_urls:
            try:
                response = self.session.get(sitemap_url, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "xml")

                for loc in soup.find_all("loc"):
                    loc_text = loc.get_text()
                    if self.blog_path in loc_text:
                        path = urlparse(loc_text).path
                        key = path.replace(self.blog_path, "").strip("/")
                        if key:
                            url_keys.append(key)

                if url_keys:
                    break
            except Exception as e:
                logger.warning(f"Could not fetch sitemap {sitemap_url}: {e}")

        return url_keys


class StaticPageScraper:
    """Scrapes static CMS pages from a Magento website."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; PageMigrationAgent/1.0)",
            "Accept": "text/html,application/json",
        })
        self.graphql_url = f"{self.base_url}/graphql"

    def fetch_page_by_url(self, page_url: str, url_key: str = "", use_playwright: bool = True) -> dict:
        """Fetch a static page.

        Priority:
        1. Playwright (full rendering with computed styles) — best fidelity
        2. CMS GraphQL — structured data but loses Page Builder CSS
        3. HTML scraping — last resort
        """
        logger.info(f"Fetching static page: {url_key or page_url}")

        # 1. Try Playwright
        if use_playwright and PLAYWRIGHT_AVAILABLE and page_url:
            pw_result = _run_playwright_scrape(page_url, base_url=self.base_url)
            if pw_result and pw_result.get("html_content"):
                pw_result["page_type"] = "static"
                pw_result["url_key"] = url_key or pw_result.get("url_key", "")
                pw_result["primary_url"] = page_url
                logger.info("Playwright scrape succeeded for %s", url_key or page_url)
                return pw_result

        # 2. Try GraphQL cmsPage
        if url_key:
            result = self._fetch_via_cms_graphql(url_key)
            if result and not result.get("error"):
                return result

        # 3. Fallback to HTML scraping
        logger.info(f"GraphQL failed for {url_key}, falling back to HTML scraping")
        return self._fetch_via_html(page_url, url_key)

    def _fetch_via_cms_graphql(self, identifier: str) -> dict | None:
        """Fetch CMS page via Magento's built-in cmsPage GraphQL query."""
        query = """
        query GetCmsPage($identifier: String!) {
            cmsPage(identifier: $identifier) {
                identifier
                url_key
                title
                content
                content_heading
                page_layout
                meta_title
                meta_description
                meta_keywords
            }
        }
        """
        try:
            response = self.session.post(
                self.graphql_url,
                json={"query": query, "variables": {"identifier": identifier}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.warning(f"CMS GraphQL errors for {identifier}: {data['errors']}")
                return None

            page_data = data.get("data", {}).get("cmsPage")
            if not page_data:
                return None

            html_content = page_data.get("content", "") or ""

            return {
                "title": page_data.get("title", ""),
                "html_content": html_content,
                "content_heading": page_data.get("content_heading", ""),
                "thumbnail": "",
                "meta_title": page_data.get("meta_title", "") or page_data.get("title", ""),
                "meta_description": page_data.get("meta_description", ""),
                "meta_keywords": page_data.get("meta_keywords", ""),
                "url_key": page_data.get("identifier", "") or page_data.get("url_key", "") or identifier,
                "page_layout": page_data.get("page_layout", ""),
                "categories": [],
                "tags": [],
                "published_at": "",
                "images": self._extract_images_from_html(html_content),
                "source": "cms_graphql",
                "page_type": "static",
            }

        except Exception as e:
            logger.warning(f"CMS GraphQL request failed for {identifier}: {e}")
            return None

    def _fetch_via_html(self, page_url: str, url_key: str = "") -> dict:
        """Fetch static page by scraping its HTML."""
        if not page_url:
            return {"error": "No URL provided", "url_key": url_key}

        try:
            response = self.session.get(page_url, timeout=30)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch HTML for {page_url}: {e}")
            return {"error": str(e), "url_key": url_key}

        soup = BeautifulSoup(response.text, "html.parser")
        return self._parse_static_page(soup, url_key, page_url)

    def _parse_static_page(self, soup: BeautifulSoup, url_key: str, url: str) -> dict:
        """Parse a static CMS page from HTML."""
        # Title
        title = ""
        for selector in ["h1.page-title span", "h1.page-title", ".page-title-wrapper h1", "h1"]:
            el = soup.select_one(selector)
            if el:
                title = el.get_text(strip=True)
                break

        # Main content area - try CMS-specific selectors first
        html_content = ""
        for selector in [
            ".cms-page-view .column.main",
            ".cms-content",
            ".page-main .column.main",
            "#maincontent .column.main",
            ".page-main",
            "#maincontent",
            "main",
        ]:
            el = soup.select_one(selector)
            if el:
                # Remove all non-content elements aggressively
                for unwanted in el.select(
                    "script, noscript, iframe, link, meta, "
                    ".breadcrumbs, .sidebar, .sidebar-main, .sidebar-additional, "
                    "nav, .nav, .navigation, .vertical-menu, "
                    "header, .header, .page-header, .pwa-header, "
                    "footer, .footer, .page-footer, .pwa-footer, "
                    ".page-title-wrapper, .modal-popup, .modal-slide, "
                    ".modals-wrapper, .loading-mask, .loader, "
                    ".minicart-wrapper, .block-search, .search-autocomplete, "
                    ".cookie-notice, .cookie-consent, #cookie-status, "
                    ".messages, .page.messages"
                ):
                    unwanted.decompose()
                # Also remove HTML comments
                from bs4 import Comment
                for comment in el.find_all(string=lambda text: isinstance(text, Comment)):
                    comment.extract()
                html_content = str(el)
                break

        # Extract thumbnail / hero image
        thumbnail = ""
        og_image = soup.select_one('meta[property="og:image"]')
        if og_image:
            thumbnail = og_image.get("content", "")

        # Meta description
        meta_desc = ""
        meta_el = soup.select_one('meta[name="description"]')
        if meta_el:
            meta_desc = meta_el.get("content", "")

        # Meta title
        meta_title = ""
        if soup.title:
            meta_title = soup.title.string or ""

        # Meta keywords
        meta_keywords = ""
        kw_el = soup.select_one('meta[name="keywords"]')
        if kw_el:
            meta_keywords = kw_el.get("content", "")

        return {
            "title": title,
            "html_content": html_content,
            "thumbnail": thumbnail,
            "meta_title": meta_title,
            "meta_description": meta_desc,
            "meta_keywords": meta_keywords,
            "url_key": url_key,
            "categories": [],
            "tags": [],
            "published_at": "",
            "images": self._extract_images_from_html(html_content),
            "source": "html",
            "page_type": "static",
        }

    def _extract_images_from_html(self, html_content: str) -> list[str]:
        """Extract all image URLs from HTML content."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "html.parser")
        images = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src:
                if not src.startswith("http"):
                    src = urljoin(self.base_url, src)
                images.append(src)

        return list(dict.fromkeys(images))
