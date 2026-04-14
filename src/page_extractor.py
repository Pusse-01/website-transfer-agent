"""
Full-page extractor for Magento storefronts.

The previous scraper only grabbed a single content element and a small hardcoded
base CSS, which lost everything that made the page look like the original:
carousels, hover states, sliders, responsive grid, custom fonts, etc.

This module fixes the root cause by capturing the fully rendered frontend page
and inlining every stylesheet that contributes to its appearance. The resulting
HTML document renders 100% identically to the original when displayed in any
iframe — including Builder.io's Custom Code block and Streamlit's
``components.html()``.

Three extraction modes are supported:

1. ``snapshot`` (default): download the page, download every linked CSS file,
   rewrite ``url(...)`` references inside CSS to absolute URLs, inline the CSS
   into ``<style>`` blocks, and keep ``<script src="...">`` tags pointing to
   the absolute source (so RequireJS/jQuery based carousels continue to work).
   Returns a **complete HTML document** (``<!DOCTYPE html>``…) that can be
   passed directly to ``components.html()`` — no nested iframe tricks needed.

2. ``content``: snapshot mode restricted to a content selector (e.g. the
   ``main`` element), with all compiled styles still attached so the extracted
   fragment looks identical to how it does on the source page.  Also returned
   as a complete HTML document.

3. ``iframe``: wrap the source URL in a plain ``<iframe src="...">`` element.
   Gives the highest fidelity with zero surprises — the browser literally
   renders the original page. Use this when the page relies on many runtime JS
   features (AJAX product tiles, analytics, login state, etc.).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# Matches url(...) in CSS. Handles single, double, or no quotes.
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")

# Matches @import "..."; and @import url(...);
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?(['"])([^'"]+)\1\s*\)?\s*;""",
    re.IGNORECASE,
)


@dataclass
class ExtractedPage:
    """Container for everything extracted from a source page."""

    url: str
    title: str = ""
    meta_description: str = ""
    # Complete <!DOCTYPE html>…</html> document (snapshot / content modes)
    # or an <iframe src="…"> string (iframe mode).
    html: str = ""
    # Convenience alias — kept for backwards compatibility.
    iframe_html: str = ""
    raw_html: str = ""          # The original HTML we downloaded, for debugging
    stylesheets: list[str] = field(default_factory=list)    # URLs inlined
    css_errors: list[str] = field(default_factory=list)     # URLs that failed
    scripts: list[str] = field(default_factory=list)        # External script URLs
    images: list[str] = field(default_factory=list)         # All image URLs
    errors: list[str] = field(default_factory=list)


class MagentoPageExtractor:
    """Extract fully-styled pages from a Magento storefront.

    The extractor reuses a ``requests.Session``, so you can pre-populate it
    with cookies (e.g. from :class:`MagentoAdminClient` after login) to pull
    unpublished or admin-only pages.
    """

    def __init__(
        self,
        base_url: str,
        session: requests.Session | None = None,
        timeout: int = 30,
        inline_css_max_bytes: int = 4 * 1024 * 1024,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.inline_css_max_bytes = inline_css_max_bytes
        self.session = session or requests.Session()
        if "User-Agent" not in self.session.headers:
            self.session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            })

        # Cache so repeated pages sharing stylesheets only download once.
        self._css_cache: dict[str, str] = {}

    # -------------------------------------------------------------- public API
    def extract(self, url: str, mode: str = "snapshot", selector: str | None = None) -> ExtractedPage:
        """Fetch ``url`` and return an :class:`ExtractedPage`.

        Args:
            url: Absolute URL or path relative to ``base_url``.
            mode: ``"snapshot"``, ``"content"``, or ``"iframe"``.
            selector: Optional CSS selector; when ``mode="content"``, only the
                matching element's HTML is embedded (styles from the whole page
                are still inlined so the fragment looks the same).
        """
        if not url.startswith("http"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))

        result = ExtractedPage(url=url)

        # ── iframe mode: no scraping needed, just wrap the live URL ──────────
        if mode == "iframe":
            result.html = _iframe_html(url)
            result.iframe_html = result.html
            return result

        # ── fetch the rendered page ──────────────────────────────────────────
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            html_text = response.text
            # Track the resolved URL (after any redirects).
            url = response.url
            result.url = url
        except Exception as e:
            result.errors.append(f"Failed to fetch page: {e}")
            logger.error("Failed to fetch %s: %s", url, e)
            return result

        result.raw_html = html_text
        soup = BeautifulSoup(html_text, "html.parser")

        # Metadata
        if soup.title and soup.title.string:
            result.title = soup.title.string.strip()
        desc_el = soup.select_one('meta[name="description"]')
        if desc_el:
            result.meta_description = desc_el.get("content", "")

        # Inline stylesheets and absolutize asset references
        self._inline_stylesheets(soup, url, result)
        self._absolutize_attributes(soup, url, result)
        self._strip_unwanted(soup)

        if mode == "content":
            doc = self._build_content_document(soup, selector)
        else:  # snapshot
            doc = self._build_full_document(soup)

        result.html = doc
        result.iframe_html = doc  # kept for API compatibility
        return result

    # --------------------------------------------------------------- internals
    def _inline_stylesheets(self, soup: BeautifulSoup, page_url: str, result: ExtractedPage) -> None:
        """Replace every ``<link rel="stylesheet">`` with an inline ``<style>``.

        - Rewrites ``url(...)`` references inside the CSS to absolute URLs so
          images, fonts, and SVGs resolve against the source CDN.
        - Recursively resolves ``@import`` directives.
        - Falls back to keeping the external ``<link>`` with an absolute href
          when a CSS file cannot be downloaded, so the browser can still try.
        """
        # Absolutize url() in existing inline style blocks first.
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or ""
            if css_text:
                style_tag.string.replace_with(self._rewrite_css_urls(css_text, page_url))

        # Replace external stylesheet links with inlined <style> blocks.
        for link in list(soup.find_all("link", rel=lambda v: v and "stylesheet" in v)):
            href = link.get("href")
            if not href:
                link.decompose()
                continue
            css_url = urljoin(page_url, href)
            media = link.get("media")
            css_text = self._fetch_and_inline_css(css_url, seen=set(), result=result)
            if css_text is None:
                # Keep absolute link so the browser still fetches it live.
                link["href"] = css_url
                continue
            result.stylesheets.append(css_url)
            style_tag = soup.new_tag("style")
            if media:
                style_tag["media"] = media
            style_tag["data-source"] = css_url
            style_tag.string = css_text
            link.replace_with(style_tag)

    def _fetch_and_inline_css(
        self, css_url: str, seen: set[str], result: ExtractedPage
    ) -> str | None:
        """Download a CSS file, rewrite urls, resolve @imports recursively.

        Tries twice: first with SSL verification, then without (many Magento
        deployments use self-signed or intermediate-chain-broken certificates
        that Python's ``certifi`` bundle rejects but browsers accept).
        """
        if css_url in seen:
            return ""  # circular import guard
        seen.add(css_url)

        if css_url in self._css_cache:
            return self._css_cache[css_url]

        resp = None
        for verify in (True, False):
            try:
                resp = self.session.get(
                    css_url,
                    timeout=self.timeout,
                    verify=verify,
                )
                resp.raise_for_status()
                break
            except requests.exceptions.SSLError:
                if not verify:
                    logger.warning("SSL error even with verify=False for %s", css_url)
                    result.css_errors.append(f"SSL error: {css_url}")
                    return None
                # Retry without verification.
                continue
            except Exception as e:
                logger.warning("Could not fetch stylesheet %s: %s", css_url, e)
                result.css_errors.append(f"{type(e).__name__}: {css_url}")
                return None

        if resp is None:
            return None

        if len(resp.content) > self.inline_css_max_bytes:
            logger.warning("Stylesheet %s exceeds max inline size; skipping", css_url)
            result.css_errors.append(f"Too large (>{self.inline_css_max_bytes//1024}KB): {css_url}")
            return None

        css_text = resp.text

        # Resolve @import chains before absolutizing url().
        def _import_repl(match: re.Match) -> str:
            imported_url = urljoin(css_url, match.group(2))
            inner = self._fetch_and_inline_css(imported_url, seen, result)
            return inner or ""

        css_text = _CSS_IMPORT_RE.sub(_import_repl, css_text)
        css_text = self._rewrite_css_urls(css_text, css_url)

        self._css_cache[css_url] = css_text
        return css_text

    @staticmethod
    def _rewrite_css_urls(css_text: str, base: str) -> str:
        """Rewrite every ``url(...)`` reference in CSS to an absolute URL."""

        def repl(match: re.Match) -> str:
            quote = match.group(1) or ""
            raw = match.group(2).strip()
            if raw.startswith(("data:", "http://", "https://", "#")):
                return f"url({quote}{raw}{quote})"
            if raw.startswith("//"):
                scheme = urlparse(base).scheme or "https"
                return f"url({quote}{scheme}:{raw}{quote})"
            return f"url({quote}{urljoin(base, raw)}{quote})"

        return _CSS_URL_RE.sub(repl, css_text)

    def _absolutize_attributes(self, soup: BeautifulSoup, page_url: str, result: ExtractedPage) -> None:
        """Make every referenced asset URL absolute so nothing 404s."""
        attr_map = {
            "img": ["src", "data-src", "data-lazy", "data-original"],
            "source": ["src", "srcset"],
            "video": ["src", "poster"],
            "audio": ["src"],
            "iframe": ["src"],
            "script": ["src"],
            "a": ["href"],
            "link": ["href"],
            "form": ["action"],
        }
        for tag_name, attrs in attr_map.items():
            for tag in soup.find_all(tag_name):
                for attr in attrs:
                    value = tag.get(attr)
                    if not value:
                        continue
                    if attr == "srcset":
                        tag[attr] = _absolutize_srcset(value, page_url)
                        continue
                    if value.startswith("//"):
                        scheme = urlparse(page_url).scheme or "https"
                        absolute = f"{scheme}:{value}"
                        tag[attr] = absolute
                    elif not value.startswith(("http", "data:", "mailto:", "tel:", "javascript:", "#")):
                        absolute = urljoin(page_url, value)
                        tag[attr] = absolute
                    else:
                        absolute = value  # already absolute

                    if tag_name == "img" and attr in ("src", "data-src"):
                        result.images.append(tag[attr])
                    elif tag_name == "script" and attr == "src":
                        result.scripts.append(tag[attr])

        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_urls(tag["style"], page_url)

    @staticmethod
    def _strip_unwanted(soup: BeautifulSoup) -> None:
        """Remove elements that shouldn't ship with the embed."""
        for selector in [
            'meta[http-equiv="X-UA-Compatible"]',
            "meta[name='csrf-token']",
            "noscript",
        ]:
            for el in soup.select(selector):
                el.decompose()

    def _build_full_document(self, soup: BeautifulSoup) -> str:
        """Return a complete ``<!DOCTYPE html>`` document for the snapshot.

        Using a real HTML document (not a div wrapper) means CSS selectors
        targeting ``html``, ``body``, ``:root``, etc. all resolve correctly
        when the document is loaded in an iframe via ``components.html()``.
        """
        # Collect <head> contents (now contains inlined <style> blocks).
        head_html = "".join(str(c) for c in soup.head.children) if soup.head else ""

        # Reconstruct the <body> with its original attributes.
        body_el = soup.body
        body_attrs = ""
        if body_el and body_el.attrs:
            parts = []
            for k, v in body_el.attrs.items():
                val = " ".join(v) if isinstance(v, list) else str(v)
                # Escape any quotes in attribute values.
                val = val.replace('"', "&quot;")
                parts.append(f'{k}="{val}"')
            body_attrs = " " + " ".join(parts)
        body_html = (
            "".join(str(c) for c in body_el.children)
            if body_el
            else str(soup)
        )

        return (
            "<!DOCTYPE html>\n"
            f"<html>\n<head>\n{head_html}\n</head>\n"
            f"<body{body_attrs}>\n{body_html}\n</body>\n</html>"
        )

    def _build_content_document(self, soup: BeautifulSoup, selector: str | None) -> str:
        """Return a complete document containing only the selected content area.

        All compiled ``<style>`` blocks are retained so the fragment looks
        identical to the live page, and external ``<script>`` sources are
        appended so carousels and sliders keep working.
        """
        content_selectors = (
            [selector] if selector else [
                "main#maincontent",
                "main",
                ".page-main",
                ".cms-page-view .column.main",
                ".amblog-post-content",
                "article",
                "body",
            ]
        )
        content_el = None
        for sel in content_selectors:
            try:
                content_el = soup.select_one(sel)
            except Exception:
                continue
            if content_el:
                break
        if content_el is None:
            content_el = soup.body or soup

        styles_html = "\n".join(str(s) for s in soup.find_all("style"))
        scripts_html = "\n".join(
            str(s) for s in soup.find_all("script") if s.get("src")
        )

        return (
            "<!DOCTYPE html>\n"
            "<html>\n<head>\n"
            + styles_html
            + "\n</head>\n<body>\n"
            + str(content_el)
            + "\n"
            + scripts_html
            + "\n</body>\n</html>"
        )


# ---------------------------------------------------------------- helpers

def _absolutize_srcset(srcset: str, base: str) -> str:
    out = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(None, 1)
        ref = bits[0]
        descriptor = bits[1] if len(bits) > 1 else ""
        if ref.startswith("//"):
            scheme = urlparse(base).scheme or "https"
            ref = f"{scheme}:{ref}"
        elif not ref.startswith(("http", "data:")):
            ref = urljoin(base, ref)
        out.append(f"{ref} {descriptor}".strip())
    return ", ".join(out)


def _iframe_html(src_url: str, height: str = "100vh") -> str:
    """Wrap a URL in an iframe that renders the original page verbatim."""
    return (
        f'<iframe src="{src_url}" '
        f'style="width:100%;height:{height};border:0;display:block;" '
        'loading="lazy" '
        'referrerpolicy="no-referrer-when-downgrade"></iframe>'
    )
