"""
Image handler module for downloading images and uploading them to Builder.io.
"""

import io
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class ImageHandler:
    """Downloads images from source and uploads them to Builder.io."""

    BUILDER_UPLOAD_URL = "https://builder.io/api/v1/upload"

    def __init__(self, builder_api_key: str, download_dir: str = "downloaded_images"):
        self.builder_api_key = builder_api_key
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; BlogMigrationAgent/1.0)",
        })
        # Cache mapping: source_url -> builder_url
        self._upload_cache: dict[str, str] = {}

    def download_image(self, image_url: str) -> Path | None:
        """Download an image from a URL to local storage."""
        if not image_url:
            return None

        try:
            parsed = urlparse(image_url)
            filename = os.path.basename(parsed.path)
            if not filename:
                filename = f"image_{hash(image_url) & 0xFFFFFFFF}.jpg"

            # Sanitize filename
            filename = re.sub(r'[^\w\-_.]', '_', filename)
            local_path = self.download_dir / filename

            if local_path.exists():
                logger.debug(f"Image already downloaded: {filename}")
                return local_path

            response = self.session.get(image_url, timeout=30, stream=True)
            response.raise_for_status()

            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"Downloaded: {filename}")
            return local_path

        except Exception as e:
            logger.error(f"Failed to download image {image_url}: {e}")
            return None

    def upload_to_builder(self, local_path: Path, filename: str = None) -> str | None:
        """Upload an image to Builder.io and return the hosted URL."""
        if not local_path or not local_path.exists():
            return None

        fname = filename or local_path.name

        # Check cache
        cache_key = str(local_path)
        if cache_key in self._upload_cache:
            return self._upload_cache[cache_key]

        try:
            # Determine content type
            ext = local_path.suffix.lower()
            content_types = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }
            content_type = content_types.get(ext, "image/jpeg")

            with open(local_path, "rb") as f:
                files = {"file": (fname, f, content_type)}
                response = requests.post(
                    self.BUILDER_UPLOAD_URL,
                    headers={"Authorization": f"Bearer {self.builder_api_key}"},
                    files=files,
                    timeout=60,
                )
                response.raise_for_status()

            result = response.json()
            # Builder.io returns the URL in the response
            builder_url = result.get("url", "")
            if not builder_url and isinstance(result, dict):
                # Try alternate response formats
                builder_url = result.get("data", {}).get("url", "")

            if builder_url:
                self._upload_cache[cache_key] = builder_url
                logger.info(f"Uploaded to Builder.io: {fname} -> {builder_url}")
                return builder_url
            else:
                logger.warning(f"Upload succeeded but no URL in response: {result}")
                return None

        except Exception as e:
            logger.error(f"Failed to upload {fname} to Builder.io: {e}")
            return None

    def process_images_in_html(self, html_content: str, base_url: str = "") -> tuple[str, list[dict]]:
        """
        Download all images in HTML content, upload to Builder.io,
        and replace image URLs in the HTML.

        Returns:
            tuple: (updated_html, list of image mappings)
        """
        if not html_content:
            return html_content, []

        soup = BeautifulSoup(html_content, "html.parser")
        image_mappings = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src:
                continue

            original_src = src
            if not src.startswith("http"):
                src = urljoin(base_url, src)

            # Download the image
            local_path = self.download_image(src)
            if not local_path:
                continue

            # Upload to Builder.io
            builder_url = self.upload_to_builder(local_path)
            if builder_url:
                # Replace the image URL in the HTML
                img["src"] = builder_url
                # Also update data-src if present
                if img.get("data-src"):
                    img["data-src"] = builder_url
                # Remove srcset as it may reference old URLs
                if img.get("srcset"):
                    del img["srcset"]

                image_mappings.append({
                    "original_url": original_src,
                    "builder_url": builder_url,
                    "local_path": str(local_path),
                })

            # Rate limit to avoid hitting API limits
            time.sleep(0.5)

        return str(soup), image_mappings

    def process_thumbnail(self, thumbnail_url: str) -> str | None:
        """Download and upload a thumbnail image to Builder.io."""
        if not thumbnail_url:
            return None

        local_path = self.download_image(thumbnail_url)
        if not local_path:
            return None

        return self.upload_to_builder(local_path)
