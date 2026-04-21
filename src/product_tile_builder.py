"""
Product-tile rebuilder.

Magento renders product carousels via Knockout.js bindings — the price,
title, badges, and cart button are populated client-side.  Our migration
pipeline strips <script> tags (Builder.io blocks them in Custom Code
blocks), so the tiles arrive at Builder.io as empty image shells.

This module detects product carousels in the scraped HTML and replaces
them with the common-slider (cs-slider) component — a zero-dependency
product carousel that works natively in Builder.io Custom Code blocks.
Product data is serialised to JSON and embedded inline in the HTML so
no external data-fetch is required at render time.

Pipeline position: run AFTER live_capture has produced the raw fragment
but BEFORE the final <style>/reinit-script wrapper is appended.  Also
run as the first step of css_processor.process_html_for_builder so the
legacy scraper path benefits too.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common-slider component loader
#
# Loads <style> and <script> blocks from src/components/common-slider.html
# so they can be injected once per page whenever a carousel is replaced.
# ---------------------------------------------------------------------------
_COMMON_SLIDER_CSS: str = ""
_COMMON_SLIDER_JS: str = ""


def _load_common_slider_component() -> tuple[str, str]:
    """Return (css_block, js_block) from common-slider.html."""
    global _COMMON_SLIDER_CSS, _COMMON_SLIDER_JS
    if _COMMON_SLIDER_CSS and _COMMON_SLIDER_JS:
        return _COMMON_SLIDER_CSS, _COMMON_SLIDER_JS

    component_path = Path(__file__).parent / "components" / "common-slider.html"
    try:
        text = component_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Could not read common-slider.html: %s", e)
        return "", ""

    # Extract content between first <style>...</style>
    style_match = re.search(r"<style>(.*?)</style>", text, re.DOTALL | re.IGNORECASE)
    css = style_match.group(1).strip() if style_match else ""

    # Extract content between first <script>...</script>
    script_match = re.search(r"<script>(.*?)</script>", text, re.DOTALL | re.IGNORECASE)
    js = script_match.group(1).strip() if script_match else ""

    _COMMON_SLIDER_CSS = css
    _COMMON_SLIDER_JS = js
    return css, js


# ---------------------------------------------------------------------------
# Price parsing helper — converts "HK$1,399.00" or "1399" → float
# ---------------------------------------------------------------------------
_PRICE_NUMERIC_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")


def _parse_price_numeric(price_str: str) -> float:
    if not price_str:
        return 0.0
    cleaned = price_str.replace(",", "")
    m = _PRICE_NUMERIC_RE.search(cleaned)
    if m:
        try:
            return float(m.group().replace(",", ""))
        except ValueError:
            pass
    return 0.0


# ---------------------------------------------------------------------------
# Convert an extracted tile dict to the cs-slider JSON item format
# ---------------------------------------------------------------------------
_BADGE_TO_TAG_COLOR = {
    "折實價": "red", "減價": "red", "特價": "red", "Sale": "red",
    "新產品": "green", "New": "green", "獨家發售": "green", "網店獨家": "green",
    "免費加長改短": "orange", "加長改短": "orange", "可自訂": "orange",
    "Hot": "orange",
}


def _tile_to_slider_item(tile: dict) -> dict:
    current = _parse_price_numeric(tile.get("new_price", ""))
    original = _parse_price_numeric(tile.get("old_price", ""))

    tags = []
    for badge in tile.get("badges", []):
        color = _BADGE_TO_TAG_COLOR.get(badge, "green")
        tags.append({"label": badge, "color": color})

    price: dict = {"currency": "HKD", "current": current or None, "prefix": ""}
    if original and original > current:
        price["original"] = original

    return {
        "title": tile.get("title", ""),
        "brand": tile.get("brand", ""),
        "url": tile.get("href", ""),
        "image": {
            "primary": tile.get("img", ""),
            "secondary": None,
            "alt": tile.get("title", ""),
        },
        "price": price,
        "tags": tags,
        "stock": {"isOutOfStock": False, "label": ""},
        "cta": None,
    }


# ---------------------------------------------------------------------------
# Render a cs-slider element with product data embedded as inline JSON.
# The common-slider.html CSS+JS is NOT included here — call
# get_common_slider_head() and prepend it once per page.
# ---------------------------------------------------------------------------
def _render_common_slider(
    tiles: list[dict],
    title: str = "",
    items_per_view: int = 4,
    items_per_view_tablet: int = 3,
    items_per_view_mobile: int = 2,
) -> str:
    items = [_tile_to_slider_item(t) for t in tiles]
    json_data = json.dumps(items, ensure_ascii=False, separators=(",", ":"))

    attrs = (
        f'class="cs-slider"'
        f' data-items-per-view="{items_per_view}"'
        f' data-items-per-view-tablet="{items_per_view_tablet}"'
        f' data-items-per-view-mobile="{items_per_view_mobile}"'
        f' data-gap="16px"'
    )
    if title:
        attrs += f' data-title="{_escape_attr(title)}"'

    return (
        f'<div {attrs}>'
        f'<script type="application/json">{json_data}</script>'
        f'</div>'
    )


def get_common_slider_head() -> str:
    """Return a <style>+<script> block to be injected once per page."""
    css, js = _load_common_slider_component()
    parts = []
    if css:
        parts.append(f"<style>\n{css}\n</style>")
    if js:
        parts.append(f"<script>\n{js}\n</script>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Image URL extraction — mirrors src/image_handler.py's precedence.
# ---------------------------------------------------------------------------
_IMG_ATTRS_IN_ORDER = (
    "data-src",
    "data-lazy",
    "data-lazy-src",
    "data-original",
    "data-pb-image-url",
    "data-pbi-src",
    "src",
)


def _img_url(img: Tag | None) -> str:
    if img is None:
        return ""
    for attr in _IMG_ATTRS_IN_ORDER:
        val = (img.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            return val
    srcset = (img.get("srcset") or "").strip()
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0].strip()
        if first and not first.startswith("data:"):
            return first
    # Last resort: even a data:URI src is better than nothing (it lets the
    # downstream image handler notice and try to resolve it).
    return (img.get("src") or "").strip()


# ---------------------------------------------------------------------------
# Text extraction helpers.
# ---------------------------------------------------------------------------
_PRICE_RE = re.compile(
    r"(?:HK\$|\$|USD|港幣)\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    return _WS_RE.sub(" ", el.get_text(" ", strip=True)).strip()


def _first_text(el: Tag, selectors: Iterable[str]) -> str:
    for sel in selectors:
        try:
            node = el.select_one(sel)
        except Exception:
            continue
        t = _text(node)
        if t:
            return t
    return ""


def _collect_prices(el: Tag) -> tuple[str, str]:
    """Return (original_price, sale_price) — either may be empty.

    Magento's price HTML is wildly inconsistent.  We look for explicit
    "old" and "special" price slots first, and fall back to scanning
    price-box children for the two distinct HK$ numbers when those
    slots aren't there.
    """
    old = _first_text(el, [
        ".old-price .price",
        ".price-was",
        "[data-price-type='oldPrice'] .price",
        "del .price",
        "del",
    ])
    new = _first_text(el, [
        ".special-price .price",
        ".price-final_price .price",
        ".price-final_price",
        "[data-price-type='finalPrice'] .price",
        ".price-box .price",
        ".price",
    ])

    # If both ended up in one blob (e.g. "HK$1,499.00 低至HK$1,399.00"),
    # split on the two HK$ tokens.
    if new and not old:
        matches = _PRICE_RE.findall(new)
        if len(matches) >= 2:
            # First is usually the "was" price, second is the "now" price
            old = f"HK${matches[0]}"
            new = f"HK${matches[-1]}"

    return old, new


_BADGE_TEXT_HINTS = (
    "折實價", "新產品", "獨家發售", "免費加長改短", "加長改短", "網店獨家",
    "可自訂", "減價", "特價", "Sale", "New", "Hot",
)


def _collect_badges(el: Tag) -> list[str]:
    """Extract short visual labels (sale tag, new product, etc.)."""
    badges: list[str] = []
    seen: set[str] = set()

    # Known badge classes first.
    for sel in (
        ".product-label",
        ".product-badge",
        ".product-badge-new",
        ".product-badge-sale",
        ".product-badges span",
        ".product-labels span",
        ".label",
        "[class*='product-badge']",
    ):
        try:
            for node in el.select(sel):
                t = _text(node)
                if t and len(t) <= 24 and t not in seen:
                    badges.append(t)
                    seen.add(t)
        except Exception:
            continue

    # Text-hint fallback: any short element whose text matches a known hint.
    if not badges:
        for node in el.find_all(["span", "div", "em", "i"], limit=200):
            t = _text(node)
            if not t or len(t) > 24:
                continue
            if any(h in t for h in _BADGE_TEXT_HINTS) and t not in seen:
                badges.append(t)
                seen.add(t)
                if len(badges) >= 3:
                    break

    return badges[:3]


# ---------------------------------------------------------------------------
# Tile detection and extraction.
# ---------------------------------------------------------------------------
_PRODUCT_TILE_SELECTORS = (
    ".product-item",
    ".item.product",
    "[data-role='product-item']",
    "li.product",
    ".product",
)


def _is_product_tile(node: Tag) -> bool:
    """Looser than a single selector — matches any plausible product card."""
    if not isinstance(node, Tag):
        return False
    classes = " ".join(node.get("class") or [])
    if "product-item" in classes or "product" == classes.strip():
        return True
    if node.get("data-role") == "product-item":
        return True
    if node.find("img") is None:
        return False
    # A product card almost always contains either a price or a product link.
    if node.select_one(".price, .price-box") is not None:
        return True
    link = node.find("a", href=True)
    if link is not None and node.select_one("img") is not None:
        # Heuristic: image wrapped in a link with a .product class somewhere
        # nearby.
        return "product" in classes.lower()
    return False


def _extract_tile(tile: Tag) -> dict:
    img = tile.select_one("img")
    img_url = _img_url(img)
    img_alt = (img.get("alt") or "").strip() if img else ""

    link = tile.find("a", href=True)
    href = (link.get("href") or "").strip() if link else ""

    brand = _first_text(tile, [
        ".product-brand",
        ".product-item-brand",
        ".brand",
        ".product-manufacturer",
    ])
    title = _first_text(tile, [
        ".product-item-name a",
        ".product-item-name",
        ".product-item-link",
        ".product-name",
        ".product-title",
        "h3",
        "h4",
    ]) or img_alt

    old_price, new_price = _collect_prices(tile)
    badges = _collect_badges(tile)

    return {
        "img": img_url,
        "href": href,
        "brand": brand,
        "title": title,
        "old_price": old_price,
        "new_price": new_price,
        "badges": badges,
    }


# ---------------------------------------------------------------------------
# HTML rendering.
# ---------------------------------------------------------------------------
# Fixed-pixel, maximally bulletproof layout.
#
# Why we don't use:
#   - flex parents with percentage widths → collapse inside narrow containers
#   - padding-bottom:100% + absolute-positioned <img> → the captured site
#     stylesheet often carries `img { height: auto !important }` which beats
#     our inline height:100% and collapses the wrapper to zero height
#   - aspect-ratio → unpredictable inside Builder.io's Custom Code block
#
# Instead: every dimension is an explicit pixel value set with !important so
# it wins against captured site CSS, Builder.io's default stylesheet, and
# premailer's post-processing.  The img is a direct child (no wrapper
# gymnastics), sized explicitly to a fixed square.
# ---------------------------------------------------------------------------
TILE_WIDTH = 220
IMG_SIZE = 200            # img is 200x200, leaving 10px padding each side of the 220 card
CARD_HEIGHT = 360         # fixed total height — never collapses
CARD_GAP = 12

_CARD_STYLE = (
    f"box-sizing:border-box !important;"
    f"display:inline-block !important;vertical-align:top !important;"
    f"width:{TILE_WIDTH}px !important;height:{CARD_HEIGHT}px !important;"
    f"background:#fff !important;border:1px solid #eee !important;"
    f"border-radius:8px !important;padding:10px !important;"
    f"text-decoration:none !important;color:inherit !important;"
    f"position:relative !important;white-space:normal !important;"
    f"overflow:hidden !important;"
    # Reset the track's font-size:0 back to a normal baseline so the text
    # inside the card renders at its declared size.
    f"font-size:14px !important;line-height:1.4 !important;"
)
_IMG_STYLE = (
    f"display:block !important;"
    f"width:{IMG_SIZE}px !important;height:{IMG_SIZE}px !important;"
    f"max-width:{IMG_SIZE}px !important;max-height:{IMG_SIZE}px !important;"
    f"object-fit:contain !important;object-position:center !important;"
    f"margin:0 auto 6px !important;background:#fafafa !important;"
    f"border-radius:6px !important;border:0 !important;"
)
_BADGE_OVERLAY_STYLE = (
    "position:absolute !important;top:12px !important;left:12px !important;"
    "display:flex !important;flex-direction:column !important;"
    "gap:4px !important;align-items:flex-start !important;"
    "z-index:2 !important;pointer-events:none !important;"
)
_BADGE_COLORS = {
    # Red for sale/discount, green for availability/new, orange for customize
    "折實價": ("#e02020", "#fff"),
    "減價": ("#e02020", "#fff"),
    "特價": ("#e02020", "#fff"),
    "Sale": ("#e02020", "#fff"),
    "新產品": ("#27ae60", "#fff"),
    "New": ("#27ae60", "#fff"),
    "獨家發售": ("#27ae60", "#fff"),
    "網店獨家": ("#27ae60", "#fff"),
    "免費加長改短": ("#fff", "#27ae60"),  # outline
    "加長改短": ("#fff", "#e67e22"),       # outline orange
    "可自訂": ("#fff", "#e67e22"),
}


def _badge_html(label: str) -> str:
    bg, fg = _BADGE_COLORS.get(label, ("#333", "#fff"))
    outline = bg == "#fff"
    border = f"1px solid {fg}" if outline else "none"
    return (
        f'<span style="display:inline-block !important;padding:2px 8px !important;'
        f'font-size:12px !important;font-weight:600 !important;'
        f'border-radius:4px !important;'
        f'background:{bg} !important;color:{fg} !important;border:{border} !important;'
        f'line-height:1.4 !important;white-space:nowrap !important;">{label}</span>'
    )


def _render_tile(t: dict) -> str:
    # Badges overlay the top-left corner (positioned absolute relative to the
    # card, which is position:relative).
    badges_overlay = ""
    if t["badges"]:
        badges_overlay = (
            f'<div style="{_BADGE_OVERLAY_STYLE}">'
            + "".join(_badge_html(b) for b in t["badges"])
            + "</div>"
        )

    brand_html = ""
    if t["brand"]:
        brand_html = (
            '<div style="font-size:12px !important;color:#888 !important;'
            'line-height:1.3 !important;margin:0 0 2px !important;'
            'white-space:nowrap !important;overflow:hidden !important;'
            'text-overflow:ellipsis !important;">'
            f'{_escape(t["brand"])}</div>'
        )

    title_html = ""
    if t["title"]:
        title_html = (
            '<div style="font-size:13px !important;color:#222 !important;'
            'line-height:1.35 !important;font-weight:500 !important;'
            'margin:0 0 6px !important;max-height:2.7em !important;'
            'overflow:hidden !important;">'
            f'{_escape(t["title"])}</div>'
        )

    price_html = ""
    if t["old_price"] or t["new_price"]:
        parts = []
        if t["old_price"]:
            parts.append(
                '<span style="font-size:12px !important;color:#aaa !important;'
                'text-decoration:line-through !important;margin-right:6px !important;'
                'white-space:nowrap !important;">'
                f'{_escape(t["old_price"])}</span>'
            )
        if t["new_price"]:
            parts.append(
                '<span style="font-size:16px !important;color:#ff6b00 !important;'
                'font-weight:700 !important;white-space:nowrap !important;">'
                f'{_escape(t["new_price"])}</span>'
            )
        price_html = (
            '<div style="margin:0 !important;line-height:1.3 !important;">'
            + "".join(parts)
            + "</div>"
        )

    img_src = t["img"] or ""
    img_tag = ""
    if img_src:
        img_tag = (
            f'<img src="{_escape_attr(img_src)}" alt="{_escape_attr(t["title"])}" '
            f'loading="lazy" '
            f'width="{IMG_SIZE}" height="{IMG_SIZE}" '
            f'style="{_IMG_STYLE}"/>'
        )

    inner = f'{img_tag}{badges_overlay}{brand_html}{title_html}{price_html}'

    if t["href"]:
        return (
            f'<a href="{_escape_attr(t["href"])}" style="{_CARD_STYLE}" '
            f'target="_blank" rel="noopener">{inner}</a>'
        )
    return f'<div style="{_CARD_STYLE}">{inner}</div>'


def _render_carousel(tiles: list[dict], slides_per_view: int = 4) -> str:
    """Render a horizontal, fixed-width carousel with prev/next buttons.

    Layout uses `display: inline-block` + `white-space: nowrap` on the track
    instead of flex.  This is dead simple and supported everywhere; flex
    containers inside Builder.io's Custom Code block were getting their
    children squashed by conflicting ancestor CSS.  With inline-block +
    fixed-pixel tile widths, the horizontal layout is immune to any
    display/flex-related ancestor interference.

    `slides_per_view` is accepted for API compatibility; it's only used to
    decide whether to scroll-snap.
    """
    # Tiles are wrapped in a <span> with margin-right for spacing (inline-block
    # doesn't honour flex `gap`). The track sets font-size:0 to eliminate the
    # whitespace between inline-blocks; each tile resets font-size back via its
    # own inline styles.
    wrapped_tiles = "".join(
        f'<span style="display:inline-block !important;vertical-align:top !important;'
        f'margin-right:{CARD_GAP}px !important;font-size:0 !important;'
        f'line-height:1 !important;">'
        + _render_tile(t)
        + "</span>"
        for t in tiles
    )

    btn_style = (
        "box-sizing:border-box !important;position:absolute !important;"
        f"top:{IMG_SIZE // 2 + 10}px !important;"  # vertically centered on the image
        "transform:translateY(-50%) !important;"
        "width:36px !important;height:36px !important;"
        "border-radius:50% !important;border:none !important;"
        "background:rgba(240,240,240,0.95) !important;color:#444 !important;"
        "font-size:20px !important;cursor:pointer !important;z-index:3 !important;"
        "text-align:center !important;line-height:36px !important;"
        "padding:0 !important;"
        "box-shadow:0 1px 4px rgba(0,0,0,0.12) !important;"
    )
    scroll_prev = (
        "var t=this.nextElementSibling;"
        "if(t){t.scrollBy({left:-t.clientWidth*0.85,behavior:'smooth'});}"
    )
    scroll_next = (
        "var t=this.previousElementSibling;"
        "if(t){t.scrollBy({left:t.clientWidth*0.85,behavior:'smooth'});}"
    )

    return (
        '<div class="pr-tile-carousel" '
        'style="box-sizing:border-box !important;position:relative !important;'
        'width:100% !important;max-width:100% !important;'
        'margin:16px 0 !important;padding:0 !important;'
        'font-size:0 !important;">'
        f'<button type="button" aria-label="Previous" '
        f'style="{btn_style}left:-8px !important;" onclick="{scroll_prev}">&#8249;</button>'
        f'<div class="pr-tile-track" '
        f'style="box-sizing:border-box !important;width:100% !important;'
        f'max-width:100% !important;white-space:nowrap !important;'
        f'overflow-x:auto !important;overflow-y:hidden !important;'
        f'padding:4px 2px 12px !important;'
        f'-webkit-overflow-scrolling:touch !important;'
        f'font-size:0 !important;">'
        f'{wrapped_tiles}'
        f'</div>'
        f'<button type="button" aria-label="Next" '
        f'style="{btn_style}right:-8px !important;" onclick="{scroll_next}">&#8250;</button>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Small HTML escape helpers (local so we don't pull in html.escape for the
# attribute-variant with quotes).
# ---------------------------------------------------------------------------
def _escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(s: str) -> str:
    return _escape(s).replace('"', "&quot;").replace("'", "&#39;")


# ---------------------------------------------------------------------------
# Carousel detection + replacement
# ---------------------------------------------------------------------------
# Covers the most common Magento / third-party carousel wrappers.  Order
# matters: specific carousel wrappers first, then generic product-grid
# containers used by related/upsell/crosssell widgets, then Magento page
# builder product widgets.
_CAROUSEL_SELECTORS = (
    # Explicit carousel libraries
    ".slick-slider",
    ".slick-initialized",
    ".owl-carousel",
    ".swiper",
    ".swiper-container",
    "[data-role='carousel']",
    "[data-slick]",
    "[data-pc-carousel-count]",
    "[data-amcarousel]",
    # Magento Page Builder product widgets (carousel OR grid appearance)
    '[data-content-type="products"]',
    '[data-content-type="product"]',
    # Magento native / widget product lists and grids — these are routinely
    # rendered as Slick carousels on pricerite.com.hk via JS, but if the
    # class that identifies them isn't `.slick-slider` itself (e.g. when the
    # wrapper stays `.products.list.items.product-items` and only the inner
    # `<ul>` becomes slick), we still need to rebuild them.
    ".products-grid",
    ".products.list",
    ".product-items",
    "ol.products",
    "ul.products",
    # Related / upsell / crosssell product blocks
    ".block.related",
    ".block.upsell",
    ".block.crosssell",
    ".block-products-list",
    ".widget-product-carousel",
)


def _find_carousels(soup: BeautifulSoup) -> list[Tag]:
    seen: set[int] = set()
    out: list[Tag] = []
    for sel in _CAROUSEL_SELECTORS:
        try:
            for node in soup.select(sel):
                if id(node) in seen:
                    continue
                # Only keep carousels that actually contain product tiles.
                if not _has_product_tiles(node):
                    continue
                # Skip nested carousels — we'll rebuild the outermost.
                if any(node is a or node in a.parents for a in out):
                    continue
                out.append(node)
                seen.add(id(node))
        except Exception:
            continue

    # Last-resort fallback: any container with 3+ product-tile descendants
    # that isn't already inside a detected carousel.  Catches pricerite's
    # custom non-class-tagged product rows.
    for tile_sel in _PRODUCT_TILE_SELECTORS:
        try:
            tiles = soup.select(tile_sel)
        except Exception:
            continue
        for tile in tiles:
            parent = tile.parent
            while parent is not None and parent.name not in (None, "[document]"):
                # Skip if this parent already lives inside a detected carousel.
                if any(parent is a or parent in a.parents for a in out):
                    break
                try:
                    sibling_tiles = parent.select(tile_sel)
                except Exception:
                    sibling_tiles = []
                if len(sibling_tiles) >= 3:
                    if id(parent) not in seen:
                        out.append(parent)
                        seen.add(id(parent))
                    break
                parent = parent.parent

    return out


def _has_product_tiles(node: Tag) -> bool:
    for sel in _PRODUCT_TILE_SELECTORS:
        try:
            if node.select_one(sel) is not None:
                return True
        except Exception:
            continue
    # Heuristic fallback: at least two images + one price.
    imgs = node.find_all("img")
    price = node.select_one(".price, .price-box")
    return len(imgs) >= 2 and price is not None


def _tiles_within(carousel: Tag) -> list[Tag]:
    """Return unique, non-clone product tiles within the carousel."""
    tiles: list[Tag] = []
    seen_keys: set[str] = set()
    for sel in _PRODUCT_TILE_SELECTORS:
        try:
            found = carousel.select(sel)
        except Exception:
            continue
        for node in found:
            classes = node.get("class") or []
            # Skip Slick clones — the class may be on the node itself or
            # on an ancestor .slick-slide wrapper.
            if "slick-cloned" in classes:
                continue
            if any("slick-cloned" in (a.get("class") or []) for a in node.parents):
                continue
            # Some themes nest product-item inside product-item; keep the
            # outermost only.
            if any(node in t.parents for t in tiles):
                continue
            # Dedupe by href + image URL — Slick sometimes duplicates even
            # non-cloned nodes.
            img = node.select_one("img")
            href = (node.find("a", href=True) or {}).get("href", "") if node.find("a", href=True) else ""
            key = (href or "") + "|" + _img_url(img)
            if key and key in seen_keys:
                continue
            seen_keys.add(key)
            tiles.append(node)
        if tiles:
            break
    return tiles


def _carousel_slides_per_view(carousel: Tag, tile_count: int) -> int:
    raw = (carousel.get("data-pc-carousel-count")
           or carousel.get("data-slides-to-show")
           or "")
    try:
        n = int(str(raw).strip())
        if 1 <= n <= 6:
            return n
    except (TypeError, ValueError):
        pass
    # Best-effort default: 4-up on desktop; but if there are very few tiles
    # show them all side-by-side.
    return min(4, max(2, tile_count))


def rebuild_product_tiles(html: str) -> str:
    """Find product carousels in the given HTML and replace them with
    cs-slider components (common-slider.html).  Returns the modified HTML;
    if no carousels are detected, the input is returned unchanged.

    The common-slider CSS+JS is injected once at the top of the output
    when at least one carousel is replaced, so the component is fully
    self-contained inside Builder.io's Custom Code block.

    Safe to call multiple times — replaced carousels carry the sentinel
    class `cs-slider` which we never re-match.
    """
    if not html or "<" not in html:
        return html
    # Cheap early-out: avoid BeautifulSoup cost when no carousel markers exist.
    if (
        "slick" not in html
        and "product-item" not in html
        and "owl-carousel" not in html
        and "swiper" not in html
    ):
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning("product_tile_builder: BeautifulSoup failed: %s", e)
        return html

    replaced = 0
    for carousel in _find_carousels(soup):
        # Skip our own rebuilt output.
        classes = carousel.get("class") or []
        if "cs-slider" in classes or "pr-tile-carousel" in classes:
            continue
        tiles_nodes = _tiles_within(carousel)
        if not tiles_nodes:
            continue
        extracted = [_extract_tile(t) for t in tiles_nodes]
        # Drop noise tiles (no image and no title).
        extracted = [t for t in extracted if t["img"] or t["title"]]
        if not extracted:
            continue

        spv = _carousel_slides_per_view(carousel, len(extracted))
        spv_tablet = min(3, spv)
        spv_mobile = min(2, spv)

        # Try to pick up a heading from a sibling/ancestor block title.
        title = ""
        for sel in (".block-title strong", ".block-title span", ".widget-title",
                    "[data-element='heading']", "h2", "h3"):
            try:
                parent = carousel.parent
                heading = parent.select_one(sel) if parent else None
                if heading:
                    title = _text(heading)
                    break
            except Exception:
                pass

        new_html = _render_common_slider(
            extracted,
            title=title,
            items_per_view=spv,
            items_per_view_tablet=spv_tablet,
            items_per_view_mobile=spv_mobile,
        )
        new_node = BeautifulSoup(new_html, "html.parser")
        carousel.replace_with(new_node)
        replaced += 1

    # Static fallback: strip Slick's pixel widths + translate3d from any
    # carousel that survived detection so CSS-only scrolling still works.
    _normalize_slick_inplace(soup)

    if replaced:
        logger.info("product_tile_builder: replaced %d carousel(s) with cs-slider", replaced)
        # Inject common-slider CSS+JS once at the beginning of the fragment
        # so the component is fully self-contained inside Builder.io.
        slider_head = get_common_slider_head()
        if slider_head:
            return slider_head + "\n" + str(soup)

    return str(soup)


# ---------------------------------------------------------------------------
# Static Slick normalizer
#
# Slick writes inline pixel widths on the track (e.g. `width:5940px`) and each
# slide (e.g. `width:244px`), plus a `transform: translate3d(-1188px,...)` on
# the track to emulate paging.  It also inserts `.slick-cloned` duplicates of
# the first/last slides for its infinite-loop illusion.  When our reinit JS
# can't run (Builder.io's sandbox blocks inline <script>, Streamlit's iframe
# blocks setTimeout on migration, etc.), these artifacts break layout: the
# track is 5× wider than its container and slides overflow to the right.
#
# Solution: strip those inline artifacts at build time.  The scoped CSS in
# live_capture._CAROUSEL_CSS_FIXES then turns the remaining markup into a
# horizontally scrollable flex row on desktop, wrapping to a column on mobile.
# ---------------------------------------------------------------------------
_SLICK_INLINE_WIDTH_RE = re.compile(r"width\s*:\s*[^;]*;?", re.IGNORECASE)
_SLICK_INLINE_TRANSFORM_RE = re.compile(r"transform\s*:\s*[^;]*;?", re.IGNORECASE)


def _strip_style_props(node: Tag, patterns: Iterable[re.Pattern]) -> None:
    style = node.get("style") or ""
    if not style:
        return
    new_style = style
    for pat in patterns:
        new_style = pat.sub("", new_style)
    new_style = re.sub(r"\s*;\s*;+", ";", new_style).strip(" ;")
    if new_style:
        node["style"] = new_style
    elif "style" in node.attrs:
        del node["style"]


def _normalize_slick_inplace(soup: BeautifulSoup) -> None:
    # Remove Slick-cloned duplicate slides — they render as a second copy of
    # the first/last products in the row.
    for clone in soup.select(".slick-cloned"):
        clone.decompose()

    # Strip pixel widths + translate3d transforms from Slick's inline styles
    # so CSS can re-size track/slides.  We skip our own rebuilt output (it
    # uses classes pr-tile-carousel / pr-tile-track, not slick-*).
    strip_width = (_SLICK_INLINE_WIDTH_RE,)
    strip_width_and_transform = (_SLICK_INLINE_WIDTH_RE, _SLICK_INLINE_TRANSFORM_RE)
    for track in soup.select(".slick-track"):
        _strip_style_props(track, strip_width_and_transform)
    for slide in soup.select(".slick-slide"):
        _strip_style_props(slide, strip_width)
    for lst in soup.select(".slick-list"):
        _strip_style_props(lst, strip_width)
