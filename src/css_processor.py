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

import html
import json
import logging
import re
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
.pagebuilder-slider {
    width: 100%;
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

/* Magento Page Builder button styles */
.pagebuilder-button-primary,
.pagebuilder-button-secondary {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 10rem;
    min-height: 2.5rem;
    padding: 0.75rem 1.5rem;
    border-radius: 10px;
    border: 1px solid #50b748;
    font-weight: 700;
    line-height: 1.2;
    text-decoration: none;
    transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease;
}
.pagebuilder-button-primary {
    background-color: #50b748;
    border-color: #50b748;
    color: #fff !important;
}
.pagebuilder-button-secondary {
    background-color: transparent;
    border-color: #50b748;
    color: #50b748 !important;
}

/* -------------------------------------------------------
   Magento Product Listing (widget / products block)
   Converts vertical list → responsive card grid
   ------------------------------------------------------- */
.products.list.items,
ol.product-items,
ul.product-items {
    display: grid !important;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 16px;
    list-style: none !important;
    padding: 0 !important;
    margin: 0 0 24px 0 !important;
}
.product-item {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    border: 1px solid #e8e8e8;
    border-radius: 8px;
    overflow: hidden;
    background: #fff;
}
.product-item-info {
    display: flex;
    flex-direction: column;
    height: 100%;
}
.product-item-photo {
    display: block;
    text-align: center;
    padding: 12px;
    background: #fafafa;
}
.product-item-photo img,
.product-item-photo .product-image-photo {
    max-width: 100% !important;
    height: 160px !important;
    object-fit: contain;
    display: block;
    margin: 0 auto;
}
.product-item-details {
    padding: 10px 12px 12px;
    flex: 1;
    display: flex;
    flex-direction: column;
}
.product-item-name {
    font-size: 13px;
    font-weight: 500;
    line-height: 1.4;
    margin-bottom: 6px;
}
.product-item-name a {
    color: #333;
    text-decoration: none;
}
.price-box {
    margin-top: auto;
}
.price-box .price {
    font-size: 15px;
    font-weight: 700;
    color: #333;
}
.product-item-actions {
    padding: 8px 12px;
    border-top: 1px solid #f0f0f0;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}
.product-badge,
.badge-new,
.badge-sale,
.product-item .new-label,
.product-item .sale-label {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    background: #50b748;
    color: #fff;
    position: relative;
}
/* Magento widget product block wrapper */
[data-content-type="products"] {
    width: 100%;
    overflow: hidden;
}
/* Remove default list counters that appear due to ol */
.products.list.items li::before,
ol.product-items li::before {
    content: none !important;
}

/* -------------------------------------------------------
   Table of Contents (TOC) box — Amasty Blog / custom
   ------------------------------------------------------- */
.amblog-toc,
.amblog-table-of-contents,
.amtoc-wrap,
.table-of-contents,
[class*="toc-box"],
[class*="toc-wrap"],
[class*="toc-container"] {
    background: #f7f7f7;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 20px 24px;
    margin-bottom: 28px;
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
}
.amblog-toc-title,
.amtoc-title,
[class*="toc-title"],
[class*="toc-heading"] {
    font-weight: 700;
    font-size: 17px;
    color: #333;
    min-width: 60px;
}
.amblog-toc-list,
.amtoc-list,
[class*="toc-list"] {
    list-style: none !important;
    padding: 0 !important;
    margin: 0 !important;
    flex: 1;
    min-width: 200px;
}
.amblog-toc-list li,
.amtoc-list li,
[class*="toc-list"] li {
    margin-bottom: 8px;
}
.amblog-toc-list a,
.amtoc-list a,
[class*="toc-list"] a {
    color: #236fa1;
    text-decoration: none;
    font-size: 14px;
    border-bottom: 1px solid #d0e8f5;
    padding-bottom: 4px;
    display: block;
}

/* -------------------------------------------------------
   Comparison / feature tables (board material tables etc.)
   ------------------------------------------------------- */
.comparison-table,
[class*="comparison"],
[data-content-type="row"] table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0 24px;
}
.comparison-table th,
.comparison-table td,
[data-content-type="row"] table th,
[data-content-type="row"] table td {
    border: 1px solid #d0d0d0;
    padding: 10px 14px;
    text-align: center;
    vertical-align: middle;
    font-size: 14px;
}
.comparison-table th,
[data-content-type="row"] table th {
    background: #f0f0f0;
    font-weight: 700;
    color: #222;
}
.comparison-table tr:nth-child(even),
[data-content-type="row"] table tr:nth-child(even) {
    background: #fafafa;
}

/* -------------------------------------------------------
   Highlighted / info boxes (green-bordered tip boxes)
   ------------------------------------------------------- */
.pagebuilder-banner-wrapper,
.info-box,
[class*="info-box"],
[class*="highlight-box"],
[class*="tip-box"] {
    border: 2px solid #50b748;
    border-radius: 8px;
    padding: 16px 20px;
    margin: 16px 0;
    background: #f8fff8;
}

/* -------------------------------------------------------
   Responsive: stack product grid to 2 cols on small screens
   ------------------------------------------------------- */
@media (max-width: 600px) {
    .products.list.items,
    ol.product-items {
        grid-template-columns: repeat(2, 1fr) !important;
    }
}
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


def _set_inline_style_property(existing: str, prop: str, value: str) -> str:
    """Set or replace a single inline style property."""
    prop_key = prop.strip().lower()
    declarations: list[tuple[str, str]] = []
    replaced = False

    for decl in (existing or "").split(";"):
        decl = decl.strip()
        if not decl or ":" not in decl:
            continue
        current_prop, current_value = decl.split(":", 1)
        current_key = current_prop.strip().lower()
        if current_key == prop_key:
            if not replaced:
                declarations.append((prop.strip(), value.strip()))
                replaced = True
        else:
            declarations.append((current_prop.strip(), current_value.strip()))

    if not replaced:
        declarations.append((prop.strip(), value.strip()))

    return "; ".join(f"{name}: {val}" for name, val in declarations) + ";"


def _safe_int(value: str | None, default: int = 1) -> int:
    """Parse a positive integer from a data attribute."""
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _extract_background_image(data_background_images: str) -> str:
    """Extract the first usable background image URL from Magento JSON."""
    if not data_background_images:
        return ""

    raw = html.unescape(data_background_images).strip()
    if not raw or raw == "{}":
        return ""

    candidates = [
        raw,
        raw.replace('\\"', '"'),
        raw.replace("'", '"'),
    ]

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            for key in ("desktop_image", "tablet_image", "mobile_image"):
                url = str(parsed.get(key, "")).strip()
                if url:
                    return url

    fallback = re.search(
        r'"(?:desktop_image|tablet_image|mobile_image)"\s*:\s*"([^"]+)"',
        raw.replace('\\"', '"'),
    )
    return fallback.group(1).strip() if fallback else ""


def _apply_background_image_styles(soup: BeautifulSoup) -> None:
    """Promote Magento background image metadata into inline CSS."""
    for el in soup.find_all(attrs={"data-background-type": "image"}):
        image_url = _extract_background_image(el.get("data-background-images", ""))
        if not image_url:
            continue
        el["style"] = _merge_inline_style(
            el.get("style", ""),
            f"background-image: url('{image_url}')"
        )


def _apply_slider_fallback_layout(soup: BeautifulSoup) -> None:
    """Render Magento Page Builder sliders as static card grids in Builder."""
    for slider in soup.find_all(attrs={"data-content-type": "slider"}):
        pc_count = _safe_int(slider.get("data-pc-carousel-count"), 1)
        gap_px = 16
        slider["style"] = _merge_inline_style(
            slider.get("style", ""),
            f"display: flex; flex-wrap: wrap; align-items: stretch; gap: {gap_px}px; width: 100%"
        )

        slide_items = [child for child in slider.children if getattr(child, "name", None)]
        if not slide_items:
            continue

        slide_width = "100%" if pc_count <= 1 else f"calc((100% - {(pc_count - 1) * gap_px}px) / {pc_count})"

        for slide in slide_items:
            style = slide.get("style", "")
            style = _set_inline_style_property(style, "box-sizing", "border-box")
            style = _set_inline_style_property(style, "display", "flex")
            style = _set_inline_style_property(style, "flex-direction", "column")
            style = _set_inline_style_property(style, "width", slide_width)
            style = _set_inline_style_property(style, "max-width", slide_width)
            style = _set_inline_style_property(style, "flex", f"0 0 {slide_width}")
            style = _set_inline_style_property(style, "margin", "0")
            slide["style"] = style


def _apply_product_listing_layout(soup: BeautifulSoup) -> None:
    """Convert Magento product listing ol/ul from vertical list to card grid.

    Magento product widgets render as <ol class="products list items product-items">
    which displays as a numbered list in plain HTML. We convert it to a CSS grid
    so it looks like the original horizontal product carousel/grid.
    """
    # Target any <ol> or <ul> that has the Magento product-items class
    for container in soup.find_all(["ol", "ul"], class_=lambda c: c and "product-items" in c):
        container["style"] = _merge_inline_style(
            container.get("style", ""),
            "display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); "
            "gap: 16px; list-style: none; padding: 0; margin: 0 0 24px 0;"
        )
        for item in container.find_all("li", class_=lambda c: c and "product-item" in c):
            item["style"] = _merge_inline_style(
                item.get("style", ""),
                "box-sizing: border-box; display: flex; flex-direction: column; "
                "border: 1px solid #e8e8e8; border-radius: 8px; overflow: hidden; background: #fff;"
            )

    # Also handle [data-content-type="products"] blocks
    for el in soup.find_all(attrs={"data-content-type": "products"}):
        el["style"] = _merge_inline_style(el.get("style", ""), "width: 100%; overflow: hidden;")


def _apply_toc_layout(soup: BeautifulSoup) -> None:
    """Detect and style Table of Contents boxes from Amasty Blog or custom HTML blocks.

    TOC boxes often have a two-column layout: heading on left, links on right.
    We detect them by common class patterns and ensure the box styling is preserved.
    """
    toc_classes = [
        "amblog-toc", "amblog-table-of-contents", "amtoc-wrap",
        "table-of-contents", "toc-box", "toc-wrap", "toc-container",
    ]
    for cls in toc_classes:
        for el in soup.find_all(class_=cls):
            el["style"] = _merge_inline_style(
                el.get("style", ""),
                "background: #f7f7f7; border: 1px solid #e0e0e0; border-radius: 8px; "
                "padding: 20px 24px; margin-bottom: 28px; "
                "display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start;"
            )

    # Generic detection: a div whose direct children are a heading + a list,
    # and the heading text looks like "Contents" / "內容" / "目录"
    toc_headings = {"內容", "内容", "目录", "Contents", "Table of Contents", "目次"}
    for div in soup.find_all("div"):
        heading = div.find(["h2", "h3", "h4", "p", "strong"])
        if not heading:
            continue
        heading_text = heading.get_text(strip=True)
        if heading_text not in toc_headings:
            continue
        lst = div.find(["ul", "ol"])
        if not lst:
            continue
        # Looks like a TOC — apply box styling if not already styled
        current_style = div.get("style", "")
        if "background" not in current_style:
            div["style"] = _merge_inline_style(
                current_style,
                "background: #f7f7f7; border: 1px solid #e0e0e0; border-radius: 8px; "
                "padding: 20px 24px; margin-bottom: 28px;"
            )
        # Remove list-style from the TOC links list
        lst["style"] = _merge_inline_style(
            lst.get("style", ""),
            "list-style: none; padding: 0; margin: 0;"
        )


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

    _apply_background_image_styles(soup)
    _apply_slider_fallback_layout(soup)
    _apply_product_listing_layout(soup)
    _apply_toc_layout(soup)


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
    base_styles = f"""<style>
{PAGEBUILDER_BASE_CSS}
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
