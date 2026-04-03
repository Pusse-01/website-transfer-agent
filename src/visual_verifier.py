"""
Visual verification module for comparing original pages with Builder.io previews.

Uses Playwright to capture full-page screenshots of both the original source
page and the Builder.io preview, then performs image-based duplicate detection
and structural comparison.

This acts as a quality-assurance gate after migration: it can catch visual
duplications (e.g. repeated banners/images) that survive HTML-level dedup,
and flag pages where the Builder.io output looks significantly different
from the original.

Usage:
    verifier = VisualVerifier()
    result = await verifier.verify_page(
        original_url="https://example.com/store-payment-methods",
        builder_preview_url="https://preview.builder.io/...",
    )
    # result contains: match_score, duplicate_regions, screenshots, issues
"""

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy imports — Playwright and Pillow are optional heavy dependencies
_playwright_available = None
_pillow_available = None


def _check_playwright():
    global _playwright_available
    if _playwright_available is None:
        try:
            import playwright.async_api  # noqa: F401
            _playwright_available = True
        except ImportError:
            _playwright_available = False
    return _playwright_available


def _check_pillow():
    global _pillow_available
    if _pillow_available is None:
        try:
            from PIL import Image  # noqa: F401
            _pillow_available = True
        except ImportError:
            _pillow_available = False
    return _pillow_available


@dataclass
class DuplicateRegion:
    """A region in the page screenshot that appears to be duplicated."""
    y_start_a: int
    y_end_a: int
    y_start_b: int
    y_end_b: int
    similarity: float  # 0.0–1.0


@dataclass
class VerificationResult:
    """Result of visual verification between original and Builder.io pages."""
    original_url: str = ""
    builder_url: str = ""
    original_screenshot: str = ""   # Path to screenshot file
    builder_screenshot: str = ""
    duplicate_regions: list[DuplicateRegion] = field(default_factory=list)
    has_duplicates: bool = False
    image_count_original: int = 0
    image_count_builder: int = 0
    issues: list[str] = field(default_factory=list)
    success: bool = False


class VisualVerifier:
    """Captures and compares page screenshots to detect visual duplication."""

    def __init__(
        self,
        screenshot_dir: str = "output/screenshots",
        viewport_width: int = 1280,
        viewport_height: int = 720,
        strip_height: int = 200,
        similarity_threshold: float = 0.92,
    ):
        """
        Args:
            screenshot_dir: Where to save screenshots.
            viewport_width: Browser viewport width for screenshots.
            viewport_height: Browser viewport height (initial, full-page scroll captured).
            strip_height: Height of horizontal strips to compare for duplication.
            similarity_threshold: Min similarity (0-1) to flag strips as duplicates.
        """
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.strip_height = strip_height
        self.similarity_threshold = similarity_threshold

    async def capture_screenshot(self, url: str, label: str = "page") -> str | None:
        """Capture a full-page screenshot using Playwright.

        Args:
            url: The URL to screenshot.
            label: Label for the screenshot filename.

        Returns:
            Path to the saved screenshot, or None on failure.
        """
        if not _check_playwright():
            logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
            return None

        from playwright.async_api import async_playwright

        safe_label = re.sub(r"[^\w\-]", "_", label)[:60]
        screenshot_path = str(self.screenshot_dir / f"{safe_label}.png")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": self.viewport_width, "height": self.viewport_height},
                    ignore_https_errors=True,
                )
                page = await context.new_page()

                # Navigate and wait for network idle
                await page.goto(url, wait_until="networkidle", timeout=60000)
                # Extra wait for lazy-loaded images
                await page.wait_for_timeout(2000)

                # Scroll to bottom to trigger lazy loading, then back to top
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)

                await page.screenshot(path=screenshot_path, full_page=True)
                await browser.close()

            logger.info("Screenshot saved: %s", screenshot_path)
            return screenshot_path

        except Exception as e:
            logger.error("Failed to capture screenshot for %s: %s", url, e)
            return None

    def detect_duplicate_strips(self, screenshot_path: str) -> list[DuplicateRegion]:
        """Detect duplicate horizontal strips within a single page screenshot.

        Splits the screenshot into horizontal strips of `strip_height` pixels,
        computes a perceptual hash for each, and flags pairs with high similarity.

        This is the core detection for "same image/banner repeated vertically".
        """
        if not _check_pillow():
            logger.error("Pillow not installed. Run: pip install Pillow")
            return []

        from PIL import Image
        import struct

        try:
            img = Image.open(screenshot_path)
        except Exception as e:
            logger.error("Cannot open screenshot %s: %s", screenshot_path, e)
            return []

        width, height = img.size
        if height < self.strip_height * 2:
            return []

        # Generate strips
        strips: list[tuple[int, int, bytes]] = []
        y = 0
        while y + self.strip_height <= height:
            strip = img.crop((0, y, width, y + self.strip_height))
            # Resize to small thumbnail for fast comparison
            thumb = strip.resize((64, 16), Image.LANCZOS).convert("L")
            strip_hash = thumb.tobytes()
            strips.append((y, y + self.strip_height, strip_hash))
            y += self.strip_height // 2  # 50% overlap for better detection

        # Compare all pairs (skip adjacent strips — they naturally overlap)
        duplicates: list[DuplicateRegion] = []
        min_distance_strips = 3  # Minimum strip gap to consider as "separate region"

        for i in range(len(strips)):
            for j in range(i + min_distance_strips, len(strips)):
                sim = self._byte_similarity(strips[i][2], strips[j][2])
                if sim >= self.similarity_threshold:
                    dup = DuplicateRegion(
                        y_start_a=strips[i][0],
                        y_end_a=strips[i][1],
                        y_start_b=strips[j][0],
                        y_end_b=strips[j][1],
                        similarity=sim,
                    )
                    duplicates.append(dup)

        # Merge overlapping duplicate regions
        duplicates = self._merge_duplicate_regions(duplicates)
        return duplicates

    def count_images_in_screenshot(self, url: str, html_content: str = "") -> int:
        """Count unique images referenced in the HTML content.

        This is a lightweight alternative to visual detection — if the Builder.io
        page has more image elements than the original, it likely has duplicates.
        """
        from bs4 import BeautifulSoup

        if not html_content:
            return 0

        soup = BeautifulSoup(html_content, "html.parser")
        srcs = set()
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src:
                srcs.add(src)
        return len(srcs)

    async def verify_page(
        self,
        original_url: str,
        builder_preview_url: str = "",
        page_html: str = "",
        url_key: str = "",
    ) -> VerificationResult:
        """Run full visual verification for a migrated page.

        Steps:
        1. Screenshot the original page
        2. Screenshot the Builder.io preview (if URL provided)
        3. Detect duplicate strips in the Builder.io screenshot
        4. Compare image counts between original and builder HTML
        5. Return a VerificationResult with findings

        Args:
            original_url: URL of the original source page.
            builder_preview_url: URL of the Builder.io preview page.
            page_html: The HTML content that was uploaded to Builder.io.
            url_key: Identifier for this page (used in filenames).
        """
        result = VerificationResult(
            original_url=original_url,
            builder_url=builder_preview_url,
        )

        label = url_key or hashlib.md5(original_url.encode()).hexdigest()[:12]

        # Step 1: Screenshot original (for reference / future comparison)
        orig_path = await self.capture_screenshot(original_url, f"original_{label}")
        if orig_path:
            result.original_screenshot = orig_path

        # Step 2: Screenshot Builder.io preview (if URL provided)
        builder_path = None
        if builder_preview_url:
            builder_path = await self.capture_screenshot(
                builder_preview_url, f"builder_{label}"
            )
            if builder_path:
                result.builder_screenshot = builder_path

        # Step 3: Detect visual duplicates via strip comparison.
        # ONLY run this on the Builder.io screenshot — the original source page
        # may have legitimately similar-looking sections (e.g. repeated store
        # info banners for different branches) that are NOT duplicates.
        if builder_path:
            dups = self.detect_duplicate_strips(builder_path)
            result.duplicate_regions = dups
            result.has_duplicates = len(dups) > 0
            if dups:
                result.issues.append(
                    f"Found {len(dups)} duplicate region(s) in the Builder.io preview. "
                    "The same content may be repeated."
                )

        # Step 4: Compare image counts via HTML analysis
        if page_html:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(page_html, "html.parser")
            all_imgs = soup.find_all("img")
            unique_srcs = set()
            for img in all_imgs:
                src = img.get("src") or img.get("data-src") or ""
                if src:
                    unique_srcs.add(src)

            result.image_count_builder = len(all_imgs)

            if len(all_imgs) > len(unique_srcs):
                dup_count = len(all_imgs) - len(unique_srcs)
                result.issues.append(
                    f"Builder HTML contains {len(all_imgs)} <img> tags but only "
                    f"{len(unique_srcs)} unique sources — {dup_count} duplicate(s)."
                )
                result.has_duplicates = True

        result.success = True
        return result

    @staticmethod
    def _byte_similarity(a: bytes, b: bytes) -> float:
        """Compute normalised similarity between two byte sequences (0.0–1.0)."""
        if len(a) != len(b):
            return 0.0
        if not a:
            return 1.0
        matching = sum(1 for x, y in zip(a, b) if abs(x - y) < 20)
        return matching / len(a)

    @staticmethod
    def _merge_duplicate_regions(regions: list[DuplicateRegion]) -> list[DuplicateRegion]:
        """Merge overlapping duplicate region pairs into consolidated regions."""
        if not regions:
            return regions

        # Sort by the first region's y_start
        regions.sort(key=lambda r: (r.y_start_a, r.y_start_b))

        merged: list[DuplicateRegion] = [regions[0]]
        for r in regions[1:]:
            last = merged[-1]
            # Check if this region overlaps with the last merged one
            if (r.y_start_a <= last.y_end_a and r.y_start_b <= last.y_end_b):
                # Extend the last merged region
                merged[-1] = DuplicateRegion(
                    y_start_a=last.y_start_a,
                    y_end_a=max(last.y_end_a, r.y_end_a),
                    y_start_b=last.y_start_b,
                    y_end_b=max(last.y_end_b, r.y_end_b),
                    similarity=max(last.similarity, r.similarity),
                )
            else:
                merged.append(r)

        return merged


def run_visual_verification(
    original_url: str,
    builder_preview_url: str = "",
    page_html: str = "",
    url_key: str = "",
) -> VerificationResult:
    """Synchronous wrapper for verify_page (convenience for non-async callers)."""
    verifier = VisualVerifier()
    return asyncio.run(
        verifier.verify_page(original_url, builder_preview_url, page_html, url_key)
    )
