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
    Convert <style> blocks to inline style= attributes using premailer,
    while PRESERVING rules that can't be inlined.

    Builder.io's Custom Code block renders valid <style> tags correctly at
    runtime (only the visual editor preview shows raw text). Premailer can
    inline simple class/tag rules, but it cannot inline:
        - @media queries (responsive breakpoints)
        - :hover / :focus / :active pseudo-class rules
        - @keyframes / @font-face
        - attribute selectors ([data-content-type=...])
    Those survive inside the `<style>` block after premailer runs. We keep
    them — they are exactly the rules that give us high-fidelity hover
    states and responsive layout.
    """
    try:
        pm = Premailer(
            html_with_styles,
            remove_classes=False,
            strip_important=False,
            keep_style_tags=True,       # Keep non-inlinable rules in <style>
            include_star_selectors=True,
            cssutils_logging_level=logging.CRITICAL,  # Suppress CSS parse warnings
        )
        return pm.transform()
    except Exception:
        # If premailer fails (malformed CSS), return the original HTML so the
        # <style> blocks still render — better than losing all styling.
        return html_with_styles


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


# Only strip positioning that actually escapes Builder.io's Custom Code
# container. `position: absolute` is essential for product badges ("New",
# "Sale"), image overlays, and caption placement — stripping it makes badges
# jump off the images and wreck the layout. Keep absolute, strip sticky/fixed.
_POSITION_BREAKS = re.compile(r'position\s*:\s*(?:sticky|fixed)', re.IGNORECASE)


def _strip_dangerous_positioning(soup: BeautifulSoup) -> None:
    """Remove position: sticky/fixed from inline styles and <style> blocks.

    Magento Page Builder sometimes emits sticky TOCs or fixed banners. Inside
    Builder.io's Custom Code container these escape the content flow and
    float above unrelated sections.

    We deliberately *keep* `position: absolute` so product badges, image
    overlays, and captions stay anchored to their parent.
    """
    for el in soup.find_all(style=True):
        style = el.get("style", "") or ""
        if _POSITION_BREAKS.search(style):
            new_style = re.sub(
                r'position\s*:\s*(sticky|fixed)\s*;?', "",
                style, flags=re.IGNORECASE,
            ).strip()
            if new_style:
                el["style"] = new_style
            else:
                del el["style"]

    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if _POSITION_BREAKS.search(css_text):
            style_tag.string = re.sub(
                r'position\s*:\s*(sticky|fixed)\s*;?', "",
                css_text, flags=re.IGNORECASE,
            )


_WIDTH_IN_DECL = re.compile(
    r'(?:^|;)\s*width\s*:\s*([^;]+?)\s*(?:;|$)',
    re.IGNORECASE,
)


def _extract_width_from_style(style: str) -> str | None:
    """Return the width declaration value from an inline style, or None."""
    if not style:
        return None
    match = _WIDTH_IN_DECL.search(style)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


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

    # Row layout — force full-width centred rows so content doesn't collapse
    # to a skinny left-aligned column inside Builder.io's Custom Code container.
    for el in soup.find_all(attrs={"data-content-type": "row"}):
        base = "box-sizing: border-box; width: 100%; max-width: 100%; margin-left: auto; margin-right: auto"
        el["style"] = _merge_inline_style(el.get("style", ""), base)
        # Inner wrapper — same full-width treatment
        inner = el.find(attrs={"data-element": "inner"})
        if inner:
            inner["style"] = _merge_inline_style(
                inner.get("style", ""),
                "width: 100%; max-width: 100%; margin-left: auto; margin-right: auto; box-sizing: border-box",
            )

    # Column group — critical for side-by-side layout
    for el in soup.find_all(attrs={"data-content-type": "column-group"}):
        el["style"] = _merge_inline_style(
            el.get("style", ""),
            "display: flex; flex-wrap: wrap; width: 100%; align-items: stretch"
        )

    # Column — individual columns within a group. Promote any width: X% into
    # a flex-basis so the percentage width is honoured inside the flex row
    # instead of collapsing to content-width.
    for el in soup.find_all(attrs={"data-content-type": "column"}):
        current = el.get("style", "")
        width_value = _extract_width_from_style(current)
        base = "box-sizing: border-box; display: flex; flex-direction: column"
        if width_value:
            base += f"; flex: 0 0 {width_value}; max-width: {width_value}"
        else:
            base += "; flex: 1 1 auto"
        el["style"] = _merge_inline_style(current, base)

    # pagebuilder-column-group / column-line classes (flex containers)
    for class_name in ("pagebuilder-column-group", "pagebuilder-column-line"):
        for el in soup.find_all(class_=class_name):
            el["style"] = _merge_inline_style(
                el.get("style", ""),
                "display: flex; flex-wrap: wrap; width: 100%; align-items: stretch"
            )

    # pagebuilder-column class (individual columns) — same flex-basis promotion
    for el in soup.find_all(class_="pagebuilder-column"):
        current = el.get("style", "")
        width_value = _extract_width_from_style(current)
        base = "box-sizing: border-box; display: flex; flex-direction: column"
        if width_value:
            base += f"; flex: 0 0 {width_value}; max-width: {width_value}"
        else:
            base += "; flex: 1 1 auto"
        el["style"] = _merge_inline_style(current, base)

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
    # NOTE: _apply_slider_fallback_layout used to convert Magento sliders
    # into static flex grids here. That killed real carousels — arrows still
    # rendered but did nothing, and the content flattened into a row of
    # static cards. For a 1:1 migration we want the slider markup preserved
    # so the carousel reinit script in live_capture.py can wire it up.
    # If you ever need the static-grid fallback back, call
    # `_apply_slider_fallback_layout(soup)` here explicitly.
    _strip_dangerous_positioning(soup)


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
    width: 100%;
    max-width: 100%;
    margin: 0 auto;
    box-sizing: border-box;
}}
.migrated-blog-content [data-content-type="row"],
.migrated-blog-content [data-content-type="row"] > [data-element="inner"] {{
    width: 100%;
    max-width: 100%;
    margin-left: auto;
    margin-right: auto;
}}
.migrated-blog-content img {{
    max-width: 100%;
    height: auto;
}}
</style>"""

    body_html = str(soup)
    # Apply BOTH wrapper classes. The live-capture path scopes its extracted
    # CSS to `.migrated-live-content`; the legacy/fallback path here scopes
    # its own base styles to `.migrated-blog-content`. Using both classes on
    # the same wrapper means either stylesheet's rules match, and we never
    # lose layout just because the two pipelines disagreed on a class name.
    full_html = f"""<div class="migrated-blog-content migrated-live-content">
{base_styles}
{body_html}
</div>"""

    # Step 4: Inline remaining CSS (class-based rules premailer CAN handle)
    inlined = _inline_css(full_html)

    # Clean up premailer artifacts
    inlined_soup = BeautifulSoup(inlined, "html.parser")

    # Remove <script>/<noscript> — they don't render safely inside Builder.io
    # Custom Code blocks (and we inject our own carousel script in
    # live_capture.py when needed).
    # We deliberately KEEP surviving <style> tags: premailer can't inline
    # @media queries, :hover / :focus rules, @keyframes, @font-face, or
    # attribute selectors — those remain in <style> blocks and are exactly
    # the high-fidelity rules we need for hover states and responsive layout.
    for tag_name in ("script", "noscript"):
        for tag in inlined_soup.find_all(tag_name):
            tag.decompose()

    # Find our wrapper div (matches either class name, since we attach both)
    content_div = (
        inlined_soup.find("div", class_="migrated-blog-content")
        or inlined_soup.find("div", class_="migrated-live-content")
    )
    if content_div:
        return str(content_div)

    return str(inlined_soup)
