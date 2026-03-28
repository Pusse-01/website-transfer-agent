"""
CSS processing and HTML sanitization for migrated content.

The source website uses Magento Page Builder which relies on:
- data-pb-style attributes with CSS selectors like #html-body [data-pb-style=XXX]
- data-content-type attributes for layout (row, column-group, column, text, etc.)
- Inline styles on some elements

When migrating to Builder.io, the #html-body selector doesn't exist, so the
embedded <style> rules don't apply and the layout collapses. This module fixes
that by rewriting the CSS selectors and adding base Page Builder layout styles.

It also sanitizes the HTML to remove scripts, navigation, and other non-content
elements that break Builder.io's Custom Code block rendering.
"""

import re
import logging
from bs4 import BeautifulSoup, Comment
from premailer import Premailer


# Base CSS that replicates Magento Page Builder layout behavior
PAGEBUILDER_BASE_CSS = """
/* Magento Page Builder base layout styles */
[data-content-type="row"] {
    box-sizing: border-box;
}
[data-content-type="row"][data-appearance="contained"] {
    max-width: 100%;
    margin: 0 auto;
}
[data-content-type="row"][data-appearance="contained"] > [data-element="inner"] {
    max-width: 100%;
}
[data-content-type="column-group"] {
    display: flex;
    flex-wrap: wrap;
}
.pagebuilder-column-group {
    display: flex;
    flex-wrap: wrap;
    width: 100%;
}
.pagebuilder-column-line {
    display: flex;
    flex-wrap: wrap;
    width: 100%;
}
.pagebuilder-column {
    box-sizing: border-box;
}
[data-content-type="text"] {
    margin-bottom: 0;
    word-wrap: break-word;
}
[data-content-type="text"] p {
    margin-bottom: 10px;
}
[data-content-type="heading"] {
    margin-bottom: 10px;
}
[data-content-type="image"] {
    margin-bottom: 10px;
}
[data-content-type="image"] img {
    max-width: 100%;
    height: auto;
}

/* Table styles */
table {
    border-collapse: collapse;
    margin-bottom: 16px;
}
table td, table th {
    padding: 8px 12px;
    vertical-align: top;
}

/* Image sizing */
img {
    max-width: 100%;
    height: auto;
}

/* General typography */
p { margin-bottom: 10px; line-height: 1.6; }
h2 { margin: 20px 0 12px; }
h3 { margin: 16px 0 10px; }
a { color: #236fa1; }
mark { padding: 2px 4px; }
"""

# Elements that should be completely removed from migrated content
UNWANTED_SELECTORS = [
    "script",
    "noscript",
    "iframe",
    "link[rel='stylesheet']",
    "link[rel='preload']",
    "meta",
    # Navigation and chrome
    ".breadcrumbs",
    ".breadcrumbs-root-o73",
    "nav",
    ".nav",
    ".navigation",
    ".vertical-menu",
    "header",
    ".header",
    ".page-header",
    ".pwa-header",
    "footer",
    ".footer",
    ".page-footer",
    ".pwa-footer",
    # Magento UI elements
    ".modal-popup",
    ".modal-slide",
    ".modals-wrapper",
    ".loading-mask",
    ".loader",
    ".page-title-wrapper",
    # Sidebar
    ".sidebar",
    ".sidebar-main",
    ".sidebar-additional",
    # Cookie/consent banners
    ".cookie-notice",
    ".cookie-consent",
    "#cookie-status",
    # Search
    ".block-search",
    ".search-autocomplete",
    # Minicart
    ".minicart-wrapper",
    # Messages
    ".messages",
    ".page.messages",
]


def sanitize_html(html_content: str) -> str:
    """
    Remove scripts, navigation, and other non-content elements from HTML.

    This is critical for Builder.io — Custom Code blocks that contain <script>
    tags render as raw text instead of executing/displaying properly.
    """
    if not html_content:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")

    # Remove all unwanted elements
    for selector in UNWANTED_SELECTORS:
        for el in soup.select(selector):
            el.decompose()

    # Remove HTML comments (often contain template markers)
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Remove elements with display:none inline style (hidden elements)
    for el in soup.find_all(style=re.compile(r'display\s*:\s*none', re.IGNORECASE)):
        el.decompose()

    # Remove empty divs that are just wrappers with no content
    for div in soup.find_all("div"):
        if not div.get_text(strip=True) and not div.find("img") and not div.find("svg"):
            # Check if it has meaningful attributes (like data-content-type)
            if not any(attr.startswith("data-") for attr in div.attrs):
                div.decompose()

    return str(soup)


def _inline_css(html_with_styles: str) -> str:
    """
    Convert <style> blocks to inline style= attributes using premailer.

    Builder.io's Custom Code block renders <style> tags as raw text in the
    editor. By inlining all CSS, we avoid this problem entirely.
    """
    try:
        pm = Premailer(
            html_with_styles,
            remove_classes=False,
            strip_important=False,
            keep_style_tags=False,      # Remove <style> after inlining
            include_star_selectors=True,
            cssutils_logging_level=logging.CRITICAL,  # Suppress CSS parse warnings
        )
        return pm.transform()
    except Exception:
        # If premailer fails (malformed CSS), strip <style> tags manually
        # and return just the HTML content — better than showing raw CSS
        soup = BeautifulSoup(html_with_styles, "html.parser")
        for style_tag in soup.find_all("style"):
            style_tag.decompose()
        return str(soup)


def process_html_for_builder(html_content: str) -> str:
    """
    Process scraped HTML so it renders correctly in Builder.io.

    1. Sanitize: remove scripts, nav, footer, and other non-content elements
    2. Rewrite: fix #html-body CSS selectors
    3. Inline: convert all <style> rules to inline style= attributes
    4. Wrap: in a styled container div
    """
    if not html_content:
        return html_content

    # Step 1: Sanitize — remove scripts and non-content elements
    html_content = sanitize_html(html_content)

    soup = BeautifulSoup(html_content, "html.parser")

    # Step 2: Fix existing <style> blocks — rewrite Magento #html-body selectors
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        fixed_css = re.sub(r'#html-body\s+', '', css_text)
        style_tag.string = fixed_css

    # Step 3: Build a full HTML document with base styles + content for inlining
    base_styles = f"""<style>
.migrated-blog-content {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, "Noto Sans TC", "PingFang HK", sans-serif;
    font-size: 16px;
    line-height: 1.7;
    color: #333;
    max-width: 100%;
    overflow-x: hidden;
}}
.migrated-blog-content img {{
    max-width: 100%;
    height: auto;
}}
{PAGEBUILDER_BASE_CSS}
</style>"""

    body_html = str(soup)
    full_html = f"""<div class="migrated-blog-content">
{base_styles}
{body_html}
</div>"""

    # Step 4: Inline all CSS — converts <style> rules to style= attributes
    # This is critical because Builder.io Custom Code blocks render <style>
    # tags as visible raw text instead of applying them
    inlined = _inline_css(full_html)

    # Clean up any premailer artifacts (it may add <html><body> wrappers)
    inlined_soup = BeautifulSoup(inlined, "html.parser")
    # Find our migrated-blog-content div
    content_div = inlined_soup.find("div", class_="migrated-blog-content")
    if content_div:
        return str(content_div)

    # Fallback: return the full inlined result
    return str(inlined_soup)
