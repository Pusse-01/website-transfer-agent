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
import html
import logging
from bs4 import BeautifulSoup, Comment
from premailer import Premailer

from .deduplication import deduplicate_content_blocks, deduplicate_similar_images

logger = logging.getLogger(__name__)


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


def _unescape_pagebuilder_html(html_content: str) -> str:
    """
    Unescape HTML entities inside Magento Page Builder HTML blocks.

    Magento's Page Builder stores custom HTML/CSS/JS inside
    data-content-type="html" blocks with escaped entities, e.g.:
        &lt;style&gt; .pgl-block { ... } &lt;/style&gt;
        &lt;script&gt; ... &lt;/script&gt;

    When fetched via GraphQL, these stay escaped. BeautifulSoup sees them
    as plain text, so sanitize_html can't find or remove the <script>/<style>
    tags. We must unescape them first so they become real HTML elements.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Find all Magento Page Builder HTML blocks
    for el in soup.find_all(attrs={"data-content-type": "html"}):
        raw_text = el.decode_contents()
        # Check if it contains escaped HTML tags
        if "&lt;" in raw_text and "&gt;" in raw_text:
            unescaped = html.unescape(raw_text)
            el.clear()
            el.append(BeautifulSoup(unescaped, "html.parser"))

    return str(soup)


def sanitize_html(html_content: str) -> str:
    """
    Remove scripts, navigation, and other non-content elements from HTML.

    This is critical for Builder.io — Custom Code blocks that contain <script>
    tags render as raw text instead of executing/displaying properly.
    """
    if not html_content:
        return html_content

    # Step 0: Unescape Magento Page Builder HTML blocks so that escaped
    # <script>/<style> tags become real elements that we can detect and remove
    html_content = _unescape_pagebuilder_html(html_content)

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
            if not any(attr.startswith("data-") for attr in (div.attrs or {})):
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


def _parse_pb_style_rules(soup: BeautifulSoup) -> dict[str, str]:
    """Extract data-pb-style CSS rules from <style> blocks.

    Parses selectors like:
        #html-body [data-pb-style="ABC123"] { display: flex; width: 50%; }
        [data-pb-style="ABC123"] { ... }

    Returns a dict mapping pb-style IDs to their CSS declarations.
    """
    rules: dict[str, str] = {}
    pb_pattern = re.compile(
        r'\[data-pb-style[=\s]*["\']?([^"\'\]\s]+)["\']?\]\s*\{([^}]+)\}',
        re.DOTALL,
    )

    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        # Strip #html-body prefix first
        css_text = re.sub(r'#html-body\s+', '', css_text)
        for match in pb_pattern.finditer(css_text):
            style_id = match.group(1)
            declarations = match.group(2).strip()
            # Merge if same ID appears multiple times
            if style_id in rules:
                rules[style_id] = rules[style_id].rstrip("; ") + "; " + declarations
            else:
                rules[style_id] = declarations

    return rules


def _merge_inline_style(existing: str, new_declarations: str) -> str:
    """Merge new CSS declarations into an existing inline style string.

    Existing declarations take precedence (don't overwrite what's already set).
    """
    existing = (existing or "").strip().rstrip(";")
    new_declarations = new_declarations.strip().rstrip(";")

    if not existing:
        return new_declarations + ";"
    if not new_declarations:
        return existing + ";"

    # Parse existing properties so we don't overwrite them
    existing_props = set()
    for decl in existing.split(";"):
        decl = decl.strip()
        if ":" in decl:
            prop = decl.split(":")[0].strip().lower()
            existing_props.add(prop)

    # Add new declarations that don't conflict
    additions = []
    for decl in new_declarations.split(";"):
        decl = decl.strip()
        if ":" in decl:
            prop = decl.split(":")[0].strip().lower()
            if prop not in existing_props:
                additions.append(decl)

    if additions:
        return existing + "; " + "; ".join(additions) + ";"
    return existing + ";"


def _apply_pagebuilder_layout_styles(soup: BeautifulSoup) -> None:
    """Apply critical Magento Page Builder layout styles directly as inline styles.

    Premailer cannot match attribute selectors like [data-content-type="column-group"],
    so we must set these styles directly on the elements before CSS inlining.

    This function handles:
    1. data-pb-style rules from <style> blocks → inline style on matching elements
    2. Base layout styles for data-content-type elements (flex, width, etc.)
    3. Column width preservation from .pagebuilder-column classes
    """
    # --- Part 1: Apply data-pb-style rules from <style> blocks ---
    # Use find_all because the same data-pb-style ID can appear on multiple
    # elements (e.g. repeated column templates with the same style ID).
    pb_rules = _parse_pb_style_rules(soup)
    for style_id, declarations in pb_rules.items():
        for el in soup.find_all(attrs={"data-pb-style": style_id}):
            existing = el.get("style", "")
            el["style"] = _merge_inline_style(existing, declarations)
            logger.debug("Applied pb-style %s: %s", style_id, declarations[:80])

    # Remove data-pb-style rules from <style> blocks now that they're inlined
    # (prevents premailer from producing broken output trying to match them)
    pb_selector_pattern = re.compile(
        r'[^{}]*\[data-pb-style[=\s][^\]]*\]\s*\{[^}]*\}\s*',
        re.DOTALL,
    )
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        cleaned = pb_selector_pattern.sub('', css_text).strip()
        if cleaned:
            style_tag.string = cleaned
        else:
            style_tag.decompose()

    # --- Part 2: Apply base layout styles to data-content-type elements ---
    # These are attribute-selector rules that premailer can't handle

    # Row layout
    for el in soup.find_all(attrs={"data-content-type": "row"}):
        base = "box-sizing: border-box"
        if el.get("data-appearance") == "contained":
            base += "; max-width: 100%; margin: 0 auto"
        el["style"] = _merge_inline_style(el.get("style", ""), base)
        # Inner wrapper
        inner = el.find(attrs={"data-element": "inner"})
        if inner:
            inner["style"] = _merge_inline_style(inner.get("style", ""), "max-width: 100%")

    # Column group — critical for side-by-side layout
    for el in soup.find_all(attrs={"data-content-type": "column-group"}):
        el["style"] = _merge_inline_style(
            el.get("style", ""),
            "display: flex; flex-wrap: wrap; width: 100%"
        )

    # Column — individual columns within a group
    for el in soup.find_all(attrs={"data-content-type": "column"}):
        el["style"] = _merge_inline_style(
            el.get("style", ""),
            "box-sizing: border-box; display: flex; flex-direction: column"
        )

    # pagebuilder-column-group / column-line classes (flex containers)
    for class_name in ("pagebuilder-column-group", "pagebuilder-column-line"):
        for el in soup.find_all(class_=class_name):
            el["style"] = _merge_inline_style(
                el.get("style", ""),
                "display: flex; flex-wrap: wrap; width: 100%"
            )

    # pagebuilder-column class (individual columns)
    for el in soup.find_all(class_="pagebuilder-column"):
        el["style"] = _merge_inline_style(
            el.get("style", ""),
            "box-sizing: border-box; display: flex; flex-direction: column"
        )

    # Button groups
    for el in soup.find_all(attrs={"data-content-type": "button-item"}):
        el["style"] = _merge_inline_style(el.get("style", ""), "display: inline-block")
    for el in soup.find_all(attrs={"data-content-type": "buttons"}):
        el["style"] = _merge_inline_style(el.get("style", ""), "display: flex; flex-wrap: wrap; gap: 10px")

    # Images
    for el in soup.find_all(attrs={"data-content-type": "image"}):
        el["style"] = _merge_inline_style(el.get("style", ""), "margin-bottom: 10px")
        for img in el.find_all("img"):
            img["style"] = _merge_inline_style(img.get("style", ""), "max-width: 100%; height: auto")

    # Text blocks
    for el in soup.find_all(attrs={"data-content-type": "text"}):
        el["style"] = _merge_inline_style(el.get("style", ""), "margin-bottom: 0; word-wrap: break-word")


def process_html_for_builder(html_content: str) -> str:
    """
    Process scraped HTML so it renders correctly in Builder.io.

    1. Sanitize: remove scripts, nav, footer, and other non-content elements
    2. Apply Page Builder layout styles directly to DOM elements
    3. Inline remaining CSS (class-based rules)
    4. Wrap in a styled container div
    """
    if not html_content:
        return html_content

    # Step 1: Sanitize — remove scripts and non-content elements
    html_content = sanitize_html(html_content)

    # Step 1b: Deduplicate content blocks (removes repeated sections/banners)
    html_content, blocks_removed = deduplicate_content_blocks(html_content)
    html_content, imgs_removed = deduplicate_similar_images(html_content)
    if blocks_removed or imgs_removed:
        logger.info("Deduplication: removed %d block(s), %d image(s)", blocks_removed, imgs_removed)

    soup = BeautifulSoup(html_content, "html.parser")

    # Step 2: Fix #html-body selectors in <style> blocks
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        fixed_css = re.sub(r'#html-body\s+', '', css_text)
        style_tag.string = fixed_css

    # Step 2b: Apply Page Builder layout styles directly to DOM elements.
    # This is critical because premailer cannot inline attribute selectors
    # like [data-content-type="column-group"] or [data-pb-style="XYZ"].
    # Without this step, all flex layouts, column widths, and backgrounds
    # are lost when <style> tags are removed.
    _apply_pagebuilder_layout_styles(soup)

    # Step 3: Build full HTML document with base styles + content for inlining.
    base_styles = """<style>
.migrated-blog-content {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, "Noto Sans TC", "PingFang HK", sans-serif;
    font-size: 16px;
    line-height: 1.7;
    color: #333;
    max-width: 100%;
    overflow-x: hidden;
}
.migrated-blog-content img {
    max-width: 100%;
    height: auto;
}
table {
    border-collapse: collapse;
    margin-bottom: 16px;
}
table td, table th {
    padding: 8px 12px;
    vertical-align: top;
}
img {
    max-width: 100%;
    height: auto;
}
p { margin-bottom: 10px; line-height: 1.6; }
h2 { margin: 20px 0 12px; }
h3 { margin: 16px 0 10px; }
a { color: #236fa1; }
mark { padding: 2px 4px; }
</style>"""

    body_html = str(soup)
    full_html = f"""<div class="migrated-blog-content">
{base_styles}
{body_html}
</div>"""

    # Step 4: Inline remaining CSS (class-based rules premailer CAN handle)
    inlined = _inline_css(full_html)

    # Clean up premailer artifacts
    inlined_soup = BeautifulSoup(inlined, "html.parser")

    # Final safety: remove any <style> or <script> tags that survived
    for tag_name in ("style", "script", "noscript"):
        for tag in inlined_soup.find_all(tag_name):
            tag.decompose()

    # Find our migrated-blog-content div
    content_div = inlined_soup.find("div", class_="migrated-blog-content")
    if content_div:
        return str(content_div)

    return str(inlined_soup)


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

    # Step 1b: Deduplicate content blocks (removes repeated sections/banners)
    html_content, blocks_removed = deduplicate_content_blocks(html_content)
    html_content, imgs_removed = deduplicate_similar_images(html_content)
    if blocks_removed or imgs_removed:
        logger.info("Deduplication: removed %d block(s), %d image(s)", blocks_removed, imgs_removed)

    soup = BeautifulSoup(html_content, "html.parser")

    # Step 2: Fix #html-body selectors in <style> blocks
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        fixed_css = re.sub(r'#html-body\s+', '', css_text)
        style_tag.string = fixed_css

    # Step 2b: Apply Page Builder layout styles directly to DOM elements.
    # This is critical because premailer cannot inline attribute selectors
    # like [data-content-type="column-group"] or [data-pb-style="XYZ"].
    # Without this step, all flex layouts, column widths, and backgrounds
    # are lost when <style> tags are removed.
    _apply_pagebuilder_layout_styles(soup)

    # Step 3: Build full HTML document with base styles + content for inlining.
    # Note: the PAGEBUILDER_BASE_CSS is now mostly applied directly above,
    # but we keep class-based rules for premailer to handle.
    base_styles = """<style>
.migrated-blog-content {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, "Noto Sans TC", "PingFang HK", sans-serif;
    font-size: 16px;
    line-height: 1.7;
    color: #333;
    max-width: 100%;
    overflow-x: hidden;
}
.migrated-blog-content img {
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
img {
    max-width: 100%;
    height: auto;
}
p { margin-bottom: 10px; line-height: 1.6; }
h2 { margin: 20px 0 12px; }
h3 { margin: 16px 0 10px; }
a { color: #236fa1; }
mark { padding: 2px 4px; }
</style>"""

    body_html = str(soup)
    full_html = f"""<div class="migrated-blog-content">
{base_styles}
{body_html}
</div>"""

    # Step 4: Inline remaining CSS (class-based rules premailer CAN handle)
    inlined = _inline_css(full_html)

    # Clean up premailer artifacts
    inlined_soup = BeautifulSoup(inlined, "html.parser")

    # Final safety: remove any <style> or <script> tags that survived
    for tag_name in ("style", "script", "noscript"):
        for tag in inlined_soup.find_all(tag_name):
            tag.decompose()

    # Find our migrated-blog-content div
    content_div = inlined_soup.find("div", class_="migrated-blog-content")
    if content_div:
        return str(content_div)

    return str(inlined_soup)
