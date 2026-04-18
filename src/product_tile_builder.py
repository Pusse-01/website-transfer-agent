"""
Product-tile rebuilder.

Magento renders product carousels via Knockout.js bindings — the price,
title, badges, and cart button are populated client-side.  Our migration
pipeline strips <script> tags (Builder.io blocks them in Custom Code
blocks), so the tiles arrive at Builder.io as empty image shells.

This module detects product carousels in the scraped HTML and replaces
them with self-contained, static HTML+CSS tiles that mimic the original
look using the data we CAN extract from the DOM at capture time (image
URL, product URL, brand, title, badges, and any visible prices).

Pipeline position: run AFTER live_capture has produced the raw fragment
but BEFORE the final <style>/reinit-script wrapper is appended.  Also
run as the first step of css_processor.process_html_for_builder so the
legacy scraper path benefits too.

The rebuilt tiles use inline styles only — no external CSS class
dependencies — so Builder.io's Custom Code container can't restyle them
by accident.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


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
# Inline-style everything: Builder.io's Custom Code container has its own
# base stylesheet and our classes could collide.  Using inline styles
# guarantees what we ship is what the user sees.

_CARD_STYLE = (
    "box-sizing:border-box;background:#fff;border:1px solid #eee;"
    "border-radius:8px;padding:12px;display:flex;flex-direction:column;"
    "gap:6px;text-decoration:none;color:inherit;position:relative;"
    "min-height:100%;"
)
_IMG_WRAP_STYLE = (
    "position:relative;width:100%;aspect-ratio:1/1;display:flex;"
    "align-items:center;justify-content:center;overflow:hidden;"
    "background:#fafafa;border-radius:6px;"
)
_IMG_STYLE = (
    "max-width:100%;max-height:100%;width:100%;height:100%;"
    "object-fit:contain;"
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
        f'<span style="display:inline-block;padding:2px 8px;'
        f'font-size:12px;font-weight:600;border-radius:4px;'
        f'background:{bg};color:{fg};border:{border};'
        f'line-height:1.4;white-space:nowrap;">{label}</span>'
    )


def _render_tile(t: dict) -> str:
    badges_html = ""
    if t["badges"]:
        badges_html = (
            '<div style="display:flex;flex-wrap:wrap;gap:4px;'
            'margin-top:4px;">'
            + "".join(_badge_html(b) for b in t["badges"])
            + "</div>"
        )

    brand_html = ""
    if t["brand"]:
        brand_html = (
            f'<div style="font-size:12px;color:#888;line-height:1.3;">'
            f'{_escape(t["brand"])}</div>'
        )

    title_html = ""
    if t["title"]:
        title_html = (
            '<div style="font-size:14px;color:#222;line-height:1.35;'
            'font-weight:500;display:-webkit-box;-webkit-line-clamp:2;'
            '-webkit-box-orient:vertical;overflow:hidden;min-height:2.7em;">'
            f'{_escape(t["title"])}</div>'
        )

    price_html = ""
    if t["old_price"] or t["new_price"]:
        parts = []
        if t["old_price"]:
            parts.append(
                '<span style="font-size:12px;color:#aaa;'
                'text-decoration:line-through;">'
                f'{_escape(t["old_price"])}</span>'
            )
        if t["new_price"]:
            parts.append(
                '<span style="font-size:16px;color:#ff6b00;font-weight:700;">'
                f'{_escape(t["new_price"])}</span>'
            )
        price_html = (
            '<div style="display:flex;flex-wrap:wrap;align-items:baseline;'
            'gap:6px;margin-top:auto;">'
            + "".join(parts)
            + "</div>"
        )

    img_src = t["img"] or ""
    img_tag = ""
    if img_src:
        img_tag = (
            f'<img src="{_escape_attr(img_src)}" alt="{_escape_attr(t["title"])}" '
            f'loading="lazy" style="{_IMG_STYLE}"/>'
        )

    inner = (
        f'<div style="{_IMG_WRAP_STYLE}">{img_tag}</div>'
        f'{brand_html}{title_html}{price_html}{badges_html}'
    )

    if t["href"]:
        return (
            f'<a href="{_escape_attr(t["href"])}" style="{_CARD_STYLE}" '
            f'target="_blank" rel="noopener">{inner}</a>'
        )
    return f'<div style="{_CARD_STYLE}">{inner}</div>'


def _render_carousel(tiles: list[dict], slides_per_view: int = 4) -> str:
    """Render a horizontal, scroll-snap carousel with prev/next buttons.

    Uses CSS scroll-snap (no JS for the scroll itself). The buttons use a
    tiny inline onclick to nudge scrollLeft; Builder.io's Custom Code
    container allows inline handlers.  The carousel also works fine
    without JS — the user can swipe/drag/wheel-scroll the track.
    """
    spv = max(1, min(6, slides_per_view))
    gap = 12
    tile_html = "".join(
        '<div style="flex:0 0 calc((100% - '
        f'{gap * (spv - 1)}px) / {spv});scroll-snap-align:start;">'
        + _render_tile(t)
        + "</div>"
        for t in tiles
    )

    btn_style = (
        "position:absolute;top:50%;transform:translateY(-50%);"
        "width:36px;height:36px;border-radius:50%;border:none;"
        "background:rgba(240,240,240,0.95);color:#444;font-size:20px;"
        "cursor:pointer;z-index:2;display:flex;align-items:center;"
        "justify-content:center;box-shadow:0 1px 4px rgba(0,0,0,0.12);"
        "line-height:1;"
    )
    # Use previousElementSibling / nextElementSibling so the buttons don't
    # depend on a unique class name — makes the output trivially robust to
    # any class-stripping sanitizer.
    scroll_prev = (
        "var t=this.nextElementSibling;"
        "t.scrollBy({left:-t.clientWidth*0.85,behavior:'smooth'});"
    )
    scroll_next = (
        "var t=this.previousElementSibling;"
        "t.scrollBy({left:t.clientWidth*0.85,behavior:'smooth'});"
    )

    return (
        '<div class="pr-tile-carousel" '
        'style="position:relative;width:100%;margin:16px 0;">'
        f'<button type="button" aria-label="Previous" '
        f'style="{btn_style}left:-8px;" onclick="{scroll_prev}">&#8249;</button>'
        f'<div '
        f'style="display:flex;gap:{gap}px;overflow-x:auto;'
        f'scroll-snap-type:x mandatory;scroll-behavior:smooth;'
        f'padding:4px 2px 12px;-webkit-overflow-scrolling:touch;'
        f'scrollbar-width:none;">'
        f'{tile_html}'
        f'</div>'
        f'<button type="button" aria-label="Next" '
        f'style="{btn_style}right:-8px;" onclick="{scroll_next}">&#8250;</button>'
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
_CAROUSEL_SELECTORS = (
    ".slick-slider",
    ".slick-initialized",
    ".owl-carousel",
    ".swiper",
    ".swiper-container",
    "[data-role='carousel']",
    "[data-slick]",
    "[data-pc-carousel-count]",
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
    self-contained static tile carousels.  Returns the modified HTML; if
    no carousels are detected, the input is returned unchanged.

    Safe to call multiple times — once rebuilt, the wrapper has class
    `pr-tile-carousel` which we never re-match.
    """
    if not html or "<" not in html:
        return html
    # Cheap early-out: if there's nothing that looks remotely like a product
    # grid, don't pay the BeautifulSoup cost.
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
        # Don't re-process our own output.
        if "pr-tile-carousel" in (carousel.get("class") or []):
            continue
        tiles_nodes = _tiles_within(carousel)
        if not tiles_nodes:
            continue
        extracted = [_extract_tile(t) for t in tiles_nodes]
        # Drop tiles that have neither image nor title (probably noise).
        extracted = [t for t in extracted if t["img"] or t["title"]]
        if not extracted:
            continue
        spv = _carousel_slides_per_view(carousel, len(extracted))
        new_html = _render_carousel(extracted, slides_per_view=spv)
        new_node = BeautifulSoup(new_html, "html.parser")
        carousel.replace_with(new_node)
        replaced += 1

    if replaced:
        logger.info("product_tile_builder: rebuilt %d product carousel(s)", replaced)

    return str(soup)
