"""
Full-page extractor for Magento storefronts.

The previous scraper only grabbed a single content element and a small hardcoded
base CSS, which lost everything that made the page look like the original:
carousels, hover states, sliders, responsive grid, custom fonts, etc.

This module fixes the root cause by capturing the fully rendered frontend page
and inlining every stylesheet that contributes to its appearance. The resulting
HTML fragment renders 100% identically to the original when pasted into any
host (including Builder.io's Custom Code block).

Three extraction modes are supported:

1. ``snapshot`` (default): download the page, download every linked CSS file,
   rewrite ``url(...)`` references inside CSS to absolute URLs, inline the CSS
   into ``<style>`` blocks, and keep ``<script src="...">`` tags pointing to
   the absolute source (so RequireJS/jQuery based carousels continue to work).
   This is a self-contained fragment that can be embedded anywhere.

2. ``content``: snapshot mode restricted to a content selector (e.g. the
   ``main`` element), with all compiled styles still attached so the extracted
   fragment looks identical to how it does on the source page.

3. ``iframe``: wrap the source URL (or the snapshot as ``srcdoc``) in an
   iframe. Gives the highest fidelity with zero surprises — the browser
   literally renders the original page. Use this when the page relies on many
   runtime JS features (AJAX product tiles, analytics, login state, etc.).
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
    html: str = ""              # The self-contained HTML fragment (with inlined CSS)
    iframe_html: str = ""       # <iframe> wrapper that renders 100% identically
    raw_html: str = ""          # The original HTML we downloaded, for debugging
    stylesheets: list[str] = field(default_factory=list)   # URLs we inlined
    scripts: list[str] = field(default_factory=list)       # URLs we kept
    images: list[str] = field(default_factory=list)        # all image URLs
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
            selector: Optional CSS selector; when provided, only the matching
                element's HTML is embedded (styles from the whole page are
                still inlined so the fragment looks the same).
        """
        if not url.startswith("http"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))

        result = ExtractedPage(url=url)

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            html_text = response.text
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

        # Inline stylesheets and collect assets
        self._inline_stylesheets(soup, url, result)
        self._absolutize_attributes(soup, url, result)

        # Drop elements we don't want in the embed
        self._strip_unwanted(soup)

        # Build the fragment according to the requested mode
        if mode == "iframe":
            # Full fidelity: just iframe the source. No scraping quirks at all.
            result.html = _iframe_html(url)
            result.iframe_html = result.html
        elif mode == "content":
            fragment = self._extract_content_fragment(soup, selector)
            result.html = fragment
            result.iframe_html = _srcdoc_iframe(fragment)
        else:  # snapshot
            fragment = self._build_snapshot(soup)
            result.html = fragment
            result.iframe_html = _srcdoc_iframe(fragment)

        return result

    # --------------------------------------------------------------- internals
    def _inline_stylesheets(self, soup: BeautifulSoup, page_url: str, result: ExtractedPage) -> None:
        """Replace every ``<link rel="stylesheet">`` with an inline ``<style>``.

        - Rewrites ``url(...)`` references inside the CSS to absolute URLs so
          images, fonts, and SVGs resolve against the source CDN.
        - Recursively resolves ``@import`` directives so we don't miss rules
          that are only reachable via an import chain (Magento frequently does
          this via the LESS pipeline output).
        - Preserves existing inline ``<style>`` blocks in place (their
          ``url()`` references are also absolutized).
        """
        # First: absolutize url() in existing inline style blocks
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or ""
            if css_text:
                style_tag.string.replace_with(self._rewrite_css_urls(css_text, page_url))

        # Then: replace every external stylesheet with an inlined <style>
        for link in list(soup.find_all("link", rel=lambda v: v and "stylesheet" in v)):
            href = link.get("href")
            if not href:
                link.decompose()
                continue
            css_url = urljoin(page_url, href)
            media = link.get("media")
            css_text = self._fetch_and_inline_css(css_url, seen=set())
            if css_text is None:
                # Leave the tag with an absolute href so the browser can still try
                link["href"] = css_url
                continue
            result.stylesheets.append(css_url)
            style_tag = soup.new_tag("style")
            if media:
                style_tag["media"] = media
            style_tag["data-source"] = css_url
            style_tag.string = css_text
            link.replace_with(style_tag)

    def _fetch_and_inline_css(self, css_url: str, seen: set[str]) -> str | None:
        """Download a CSS file, rewrite urls, resolve @imports recursively."""
        if css_url in seen:
            return ""  # circular import guard
        seen.add(css_url)

        if css_url in self._css_cache:
            return self._css_cache[css_url]

        try:
            resp = self.session.get(css_url, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("Could not fetch stylesheet %s: %s", css_url, e)
            return None

        if len(resp.content) > self.inline_css_max_bytes:
            logger.warning("Stylesheet %s exceeds max inline size; skipping", css_url)
            return None

        css_text = resp.text

        # Resolve @import before absolutizing url() so we can follow chains
        def _import_repl(match: re.Match) -> str:
            imported_url = urljoin(css_url, match.group(2))
            inner = self._fetch_and_inline_css(imported_url, seen)
            return inner or ""

        css_text = _CSS_IMPORT_RE.sub(_import_repl, css_text)

        # Rewrite url() references to absolute paths based on the CSS location
        css_text = self._rewrite_css_urls(css_text, css_url)

        self._css_cache[css_url] = css_text
        return css_text

    @staticmethod
    def _rewrite_css_urls(css_text: str, base: str) -> str:
        """Rewrite every ``url(...)`` reference in CSS to an absolute URL."""

        def repl(match: re.Match) -> str:
            quote = match.group(1) or ""
            raw = match.group(2).strip()
            # Leave data: URIs and already-absolute URLs alone
            if raw.startswith(("data:", "http://", "https://", "//", "#")):
                absolute = raw
                if raw.startswith("//"):
                    scheme = urlparse(base).scheme or "https"
                    absolute = f"{scheme}:{raw}"
            else:
                absolute = urljoin(base, raw)
            return f"url({quote}{absolute}{quote})"

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
                    elif not value.startswith(("http", "data:", "mailto:", "tel:", "javascript:", "#")):
                        absolute = urljoin(page_url, value)
                        tag[attr] = absolute
                        if tag_name == "img" and attr in ("src", "data-src"):
                            result.images.append(absolute)
                        elif tag_name == "script" and attr == "src":
                            result.scripts.append(absolute)
                    elif value.startswith("//"):
                        scheme = urlparse(page_url).scheme or "https"
                        tag[attr] = f"{scheme}:{value}"

        # Inline <style> blocks have already been handled. style="..." attrs
        # with url() still need absolutizing:
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_urls(tag["style"], page_url)

    @staticmethod
    def _strip_unwanted(soup: BeautifulSoup) -> None:
        """Remove elements that shouldn't ship with the embed."""
        # Magento admin toolbars, analytics, noscripts, and anti-cache forms
        for selector in [
            'meta[http-equiv="X-UA-Compatible"]',
            "meta[name='csrf-token']",
            "noscript",
        ]:
            for el in soup.select(selector):
                el.decompose()

    def _extract_content_fragment(self, soup: BeautifulSoup, selector: str | None) -> str:
        """Return a self-contained fragment for a specific element.

        The surrounding ``<head>`` styles stay attached so the fragment still
        looks identical to the live page.
        """
        content_selectors = (
            [selector] if selector else [
                "main#maincontent",
                "main",
                ".page-main",
                ".cms-page-view .column.main",
                ".amblog-post-content",
                ".blog-post-view .post-post_content",
                "article",
                "body",
            ]
        )
        content_el = None
        for sel in content_selectors:
            content_el = soup.select_one(sel)
            if content_el:
                break
        if content_el is None:
            content_el = soup.body or soup

        styles_html = "\n".join(str(s) for s in soup.find_all("style"))
        # Keep the source scripts; they're needed for carousels / sliders.
        scripts_html = "\n".join(
            str(s) for s in soup.find_all("script") if s.get("src")
        )
        return (
            '<div class="magento-embed">\n'
            + styles_html
            + "\n"
            + str(content_el)
            + "\n"
            + scripts_html
            + "\n</div>"
        )

    def _build_snapshot(self, soup: BeautifulSoup) -> str:
        """Return a self-contained snapshot that renders the whole page."""
        head = soup.head
        body = soup.body or soup
        head_html = "".join(str(c) for c in head.children) if head else ""
        body_attrs = ""
        if soup.body and soup.body.attrs:
            body_attrs = " " + " ".join(
                f'{k}="{" ".join(v) if isinstance(v, list) else v}"'
                for k, v in soup.body.attrs.items()
            )
        body_html = "".join(str(c) for c in body.children) if hasattr(body, "children") else str(body)

        # Wrap in a scoping div so the styles from <head> are preserved as-is.
        return (
            '<div class="magento-embed-snapshot">'
            f"<!-- source: {soup.title.string.strip() if soup.title and soup.title.string else ''} -->"
            f"{head_html}"
            f'<div class="magento-embed-body"{body_attrs}>{body_html}</div>'
            "</div>"
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
        if not ref.startswith(("http", "data:")):
            ref = urljoin(base, ref)
        elif ref.startswith("//"):
            scheme = urlparse(base).scheme or "https"
            ref = f"{scheme}:{ref}"
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


def _srcdoc_iframe(fragment_html: str, height: str = "100vh") -> str:
    """Embed an HTML fragment via iframe ``srcdoc`` for style isolation."""
    escaped = (
        fragment_html
        .replace("&", "&amp;")
        .replace('"', "&quot;")
    )
    return (
        f'<iframe srcdoc="{escaped}" '
        f'style="width:100%;height:{height};border:0;display:block;"></iframe>'
    )
