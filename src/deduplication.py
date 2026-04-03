"""
Content block deduplication module.

Detects and removes duplicate content sections in HTML before uploading
to Builder.io. This handles cases where the source page (e.g. Magento
Page Builder) contains repeated blocks — such as the same banner image
or info section appearing multiple times — that the per-image dedup in
image_handler.py cannot catch because they are structurally separate
HTML subtrees that happen to contain identical content.

Strategy:
1. Parse the HTML into top-level content blocks (using Magento Page Builder
   data-content-type attributes, or falling back to direct children of the
   content root).
2. Compute a normalised content fingerprint for each block.
3. Remove later occurrences of blocks whose fingerprint matches an earlier
   block, keeping only the first occurrence.
"""

import hashlib
import logging
import re
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


def _normalise_text(text: str) -> str:
    """Collapse whitespace and lowercase for comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _fingerprint(element: Tag) -> str:
    """Compute a content fingerprint for an HTML element.

    The fingerprint is based on:
    - The normalised visible text content
    - All image src/data-src URLs present
    - Background-image URLs in inline styles

    This catches both text-identical and image-identical blocks while
    being tolerant of minor whitespace / attribute order differences.
    """
    parts: list[str] = []

    # Visible text
    text = _normalise_text(element.get_text())
    if text:
        parts.append(f"text:{text}")

    # Image sources (sorted for order-independence within a block)
    img_srcs = sorted(
        (img.get("src") or img.get("data-src") or "")
        for img in element.find_all("img")
    )
    for src in img_srcs:
        if src:
            parts.append(f"img:{src.strip()}")

    # Background-image URLs
    bg_re = re.compile(r'url\(["\']?(.*?)["\']?\)')
    for tag in element.find_all(style=True):
        style = tag.get("style", "")
        for match in bg_re.finditer(style):
            url = match.group(1).strip()
            if url:
                parts.append(f"bg:{url}")

    combined = "|".join(parts)
    return hashlib.md5(combined.encode()).hexdigest()


def _extract_content_blocks(soup: BeautifulSoup) -> list[Tag]:
    """Extract the top-level content blocks from HTML.

    Magento Page Builder uses data-content-type="row" as the main
    structural unit. If those exist, we treat each row as a block.
    Otherwise, we fall back to direct children of the root element.
    """
    # Strategy 1: Magento Page Builder rows
    rows = soup.find_all(attrs={"data-content-type": "row"})
    if rows:
        return rows

    # Strategy 2: Top-level divs / sections that are direct children of root
    root = soup.find("body") or soup.find("div") or soup
    blocks = [
        child for child in root.children
        if isinstance(child, Tag) and child.name in (
            "div", "section", "article", "table", "figure",
            "p", "h1", "h2", "h3", "h4", "h5", "h6",
        )
    ]
    return blocks


def _block_has_meaningful_content(tag: Tag) -> bool:
    """Return True if the block has enough content to be worth fingerprinting."""
    text = tag.get_text(strip=True)
    has_images = bool(tag.find("img"))
    has_bg = bool(tag.find(style=re.compile(r"background")))
    return bool(text) or has_images or has_bg


def deduplicate_content_blocks(html_content: str) -> tuple[str, int]:
    """Remove duplicate content blocks from HTML.

    Args:
        html_content: The HTML string to deduplicate.

    Returns:
        A tuple of (deduplicated_html, number_of_blocks_removed).
    """
    if not html_content:
        return html_content, 0

    soup = BeautifulSoup(html_content, "html.parser")
    blocks = _extract_content_blocks(soup)

    if len(blocks) <= 1:
        return html_content, 0

    seen_fingerprints: dict[str, Tag] = {}
    removed = 0

    for block in blocks:
        if not _block_has_meaningful_content(block):
            continue

        fp = _fingerprint(block)

        if fp in seen_fingerprints:
            logger.info(
                "Removing duplicate content block (fingerprint %s). "
                "Text preview: %.80s...",
                fp[:8],
                _normalise_text(block.get_text())[:80],
            )
            block.decompose()
            removed += 1
        else:
            seen_fingerprints[fp] = block

    if removed:
        logger.info("Deduplicated %d duplicate content block(s)", removed)

    return str(soup), removed


def deduplicate_similar_images(html_content: str) -> tuple[str, int]:
    """Remove near-duplicate image blocks that share the same visual content.

    Unlike image_handler's per-src dedup, this works on the *rendered*
    image URL (which may already be a Builder.io URL after upload) and
    removes entire parent wrapper elements, not just the <img> tag.

    This is a second-pass safety net that runs after image processing.
    """
    if not html_content:
        return html_content, 0

    soup = BeautifulSoup(html_content, "html.parser")
    images = soup.find_all("img")

    seen_srcs: dict[str, Tag] = {}
    removed = 0

    for img in images:
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue

        if src in seen_srcs:
            # Find the nearest block-level wrapper
            wrapper = _find_block_wrapper(img)
            target = wrapper or img
            logger.info("Removing duplicate image (src: %.60s...)", src[:60])
            target.decompose()
            removed += 1
        else:
            seen_srcs[src] = img

    if removed:
        logger.info("Removed %d duplicate image(s) in second-pass dedup", removed)

    return str(soup), removed


def _find_block_wrapper(img: Tag) -> Tag | None:
    """Walk up from an <img> to find the highest wrapper that only contains this image."""
    wrapper = None
    current = img.parent
    stop_tags = {None, "[document]", "body", "html", "article", "section", "main"}

    while current and current.name not in stop_tags:
        imgs_inside = current.find_all("img")
        if len(imgs_inside) == 1 and imgs_inside[0] is img:
            wrapper = current
            current = current.parent
        else:
            break

    return wrapper
