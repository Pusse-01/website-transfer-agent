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
            # Use a realistic browser UA so hotlink-protection checks pass.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Cache mapping: source_url -> builder_url
        self._upload_cache: dict[str, str] = {}

    def download_image(self, image_url: str, page_url: str = "") -> Path | None:
        """Download an image from a URL to local storage."""
        if not image_url:
            return None

        try:
            parsed = urlparse(image_url)
            filename = os.path.basename(parsed.path)
            if not filename:
                filename = f"image_{hash(image_url) & 0xFFFFFFFF}.jpg"

            # Sanitize filename and deduplicate using a URL-based suffix so
            # two different images with the same base name don't collide.
            filename = re.sub(r'[^\w\-_.]', '_', filename)
            _url_hash = format(hash(image_url) & 0xFFFF, 'x')
            name, _, ext = filename.rpartition('.')
            filename = f"{name}_{_url_hash}.{ext}" if ext else f"{filename}_{_url_hash}"
            local_path = self.download_dir / filename

            if local_path.exists():
                logger.debug(f"Image already downloaded: {filename}")
                return local_path

            # Use the page URL as Referer when available — CDN hotlink rules
            # often check that the Referer matches the site domain AND the
            # specific page. Falling back to just the origin root still passes
            # most basic checks.
            if page_url:
                referer = page_url
            else:
                parsed_ref = urlparse(image_url)
                referer = f"{parsed_ref.scheme}://{parsed_ref.netloc}/"
            response = self.session.get(
                image_url, timeout=30, stream=True,
                headers={"Referer": referer},
            )
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

            upload_url = f"{self.BUILDER_UPLOAD_URL}?name={fname}"
            with open(local_path, "rb") as f:
                response = requests.post(
                    upload_url,
                    headers={
                        "Authorization": f"Bearer {self.builder_api_key}",
                        "Content-Type": content_type,
                    },
                    data=f,
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

    def _resolve_and_upload_image(self, src: str, base_url: str, page_url: str = "") -> str | None:
        """Resolve a source image URL, download it, and upload to Builder.io.

        Returns the Builder.io URL on success, or None on failure.
        Uses an in-memory URL cache to avoid re-uploading the same source URL.
        """
        if not src:
            return None

        resolved = src if src.startswith("http") else urljoin(base_url, src)

        # Check URL-level cache (different from the local-path upload cache)
        if resolved in self._upload_cache:
            return self._upload_cache[resolved]

        local_path = self.download_image(resolved, page_url=page_url)
        if not local_path:
            return None

        builder_url = self.upload_to_builder(local_path)
        if builder_url:
            self._upload_cache[resolved] = builder_url
            time.sleep(0.5)  # Rate limit
        return builder_url

    def _is_image_url(self, url: str) -> bool:
        """Check if a URL points to an image file."""
        image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico')
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in image_extensions)

    def process_images_in_html(self, html_content: str, base_url: str = "", page_url: str = "") -> tuple[str, list[dict]]:
        """
        Download all images in HTML content, upload to Builder.io,
        and replace image URLs in the HTML.

        Handles:
        - <img> tags (src, data-src, srcset) with deduplication
        - <source> tags inside <picture>
        - <a> tags linking to image files
        - Inline style background-image URLs

        Returns:
            tuple: (updated_html, list of image mappings)
        """
        if not html_content:
            return html_content, []

        soup = BeautifulSoup(html_content, "html.parser")
        image_mappings = []
        url_mapping: dict[str, str] = {}  # original_resolved -> builder_url

        # --- Pass 1: Process <img> tags and deduplicate ---
        seen_img_srcs: dict[str, object] = {}  # resolved_src -> first <img> element
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src:
                continue

            # Slick carousel and other lazy loaders set src to a tiny blank data:
            # URI placeholder (e.g. data:image/gif;base64,...) with the real URL
            # in data-src / data-lazy / data-original. Prefer the real URL.
            if src.startswith("data:"):
                lazy_src = (
                    img.get("data-src")
                    or img.get("data-lazy")
                    or img.get("data-original")
                    or ""
                )
                if lazy_src:
                    src = lazy_src
                else:
                    continue  # blank placeholder with no real fallback — skip

            resolved_src = src if src.startswith("http") else urljoin(base_url, src)

            # Remove duplicate <img> elements that share the same source image.
            # We match on the *original* resolved URL (before Builder.io upload
            # rewrites the src), so this catches duplicates reliably.
            if resolved_src in seen_img_srcs:
                block_parent = self._find_image_wrapper(img)
                if block_parent:
                    logger.info("Removing duplicate image block (src: %.60s...)", resolved_src[:60])
                    block_parent.decompose()
                else:
                    logger.info("Removing duplicate <img> (src: %.60s...)", resolved_src[:60])
                    img.decompose()
                continue

            seen_img_srcs[resolved_src] = img
            original_src = src

            builder_url = self._resolve_and_upload_image(src, base_url, page_url=page_url)
            if builder_url:
                img["src"] = builder_url
                if img.get("data-src"):
                    img["data-src"] = builder_url
                if img.get("srcset"):
                    del img["srcset"]

                url_mapping[resolved_src] = builder_url
                image_mappings.append({
                    "original_url": original_src,
                    "builder_url": builder_url,
                    "local_path": str(self.download_dir / re.sub(r'[^\w\-_.]', '_', os.path.basename(urlparse(resolved_src).path) or "image")),
                })

        # --- Pass 2: Process <source> tags inside <picture> ---
        for source_tag in soup.find_all("source"):
            srcset = source_tag.get("srcset", "")
            if srcset:
                src_url = srcset.split(",")[0].strip().split()[0]
                builder_url = self._resolve_and_upload_image(src_url, base_url)
                if builder_url:
                    source_tag["srcset"] = builder_url

        # --- Pass 3: Replace image URLs in <a href> ---
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if self._is_image_url(href):
                resolved_href = href if href.startswith("http") else urljoin(base_url, href)
                if resolved_href in url_mapping:
                    a_tag["href"] = url_mapping[resolved_href]
                else:
                    builder_url = self._resolve_and_upload_image(href, base_url)
                    if builder_url:
                        a_tag["href"] = builder_url

        # --- Pass 4: Replace image URLs in inline style background-image ---
        bg_pattern = re.compile(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)')
        for tag in soup.find_all(style=True):
            style = tag["style"]
            if "url(" in style:
                def replace_bg_url(match):
                    old_url = match.group(1)
                    if self._is_image_url(old_url):
                        if old_url in url_mapping:
                            return f'url("{url_mapping[old_url]}")'
                        new_url = self._resolve_and_upload_image(old_url, base_url)
                        if new_url:
                            url_mapping[old_url] = new_url
                            return f'url("{new_url}")'
                    return match.group(0)
                tag["style"] = bg_pattern.sub(replace_bg_url, style)

        # --- Pass 5: Replace background-image URLs inside <style> blocks -------
        # The live-capture path embeds a large <style> block whose rules may
        # include background-image: url("https://pricerite.com.hk/...") for
        # hero banners, row backgrounds, and sliders. Those URLs aren't touched
        # by the inline-style pass (Pass 4) above, so they'd remain pointing to
        # the original domain where hotlink protection can block them at render
        # time inside Builder.io.
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or ""
            if not css_text or "url(" not in css_text:
                continue

            def replace_style_bg_url(match):
                old_url = match.group(1)
                if not self._is_image_url(old_url):
                    return match.group(0)
                if old_url in url_mapping:
                    return f'url("{url_mapping[old_url]}")'
                new_url = self._resolve_and_upload_image(old_url, base_url)
                if new_url:
                    url_mapping[old_url] = new_url
                    return f'url("{new_url}")'
                return match.group(0)

            style_tag.string = bg_pattern.sub(replace_style_bg_url, css_text)

        # --- Pass 6: Stamp referrerpolicy on any <img> still pointing to an
        #    external (non-Builder CDN) domain. Builder.io's renderer sends
        #    "Referer: https://builder.io/" when fetching images, which most
        #    hotlink-protection setups block as a foreign referrer.  With
        #    referrerpolicy="no-referrer" the browser sends no Referer header
        #    and CDNs that allow empty-referrer requests serve the image. ---
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and "cdn.builder.io" not in src:
                if not img.get("referrerpolicy"):
                    img["referrerpolicy"] = "no-referrer"
                if not img.get("crossorigin"):
                    img["crossorigin"] = "anonymous"

        return str(soup), image_mappings

    def _find_image_wrapper(self, img) -> object | None:
        """Find the nearest block-level parent that wraps primarily this image.

        Walk up from the <img> and return the highest ancestor that contains
        only this single image (removing it removes the whole block).
        """
        wrapper = None
        current = img.parent
        while current and current.name not in (None, '[document]', 'body', 'html', 'article', 'section', 'main'):
            imgs_inside = current.find_all("img")
            if len(imgs_inside) == 1 and imgs_inside[0] is img:
                wrapper = current
                current = current.parent
            else:
                break
        return wrapper

    def rewrite_internal_links(self, html_content: str, source_base_url: str) -> str:
        """Rewrite <a href> links that point at the old source domain.

        Two goals:
          1. Strip the source origin so migrated pages don't link back to the
             old Magento platform.
          2. Drop the ``.html`` suffix so the new site serves extension-free
             URLs (the user asked for this explicitly).

        External links, fragment-only anchors, and non-HTTP schemes are left
        unchanged. Image links (handled elsewhere) are also skipped to avoid
        double-processing.
        """
        if not html_content or not source_base_url:
            return html_content

        parsed_source = urlparse(source_base_url)
        source_host = (parsed_source.hostname or "").lower().lstrip("www.")
        if not source_host:
            return html_content

        soup = BeautifulSoup(html_content, "html.parser")

        for a in soup.find_all("a", href=True):
            raw = a["href"].strip()
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            # Skip image links — those are already handled by the image
            # pipeline and point to the Builder CDN by now.
            if self._is_image_url(raw):
                continue

            # Resolve the href against the source so protocol-relative and
            # root-relative links are normalised before we inspect the host.
            resolved = raw if raw.startswith(("http://", "https://")) else urljoin(source_base_url, raw)
            parsed = urlparse(resolved)
            href_host = (parsed.hostname or "").lower().lstrip("www.")

            # Only rewrite links that point at the old Magento domain.
            if href_host and href_host != source_host:
                continue

            path = parsed.path or "/"
            # Strip the ``.html`` suffix from the final segment only.
            path = re.sub(r"\.html(/?)$", r"\1", path, flags=re.IGNORECASE)
            new_href = path
            if parsed.query:
                new_href += "?" + parsed.query
            if parsed.fragment:
                new_href += "#" + parsed.fragment
            a["href"] = new_href

        return str(soup)

    def process_thumbnail(self, thumbnail_url: str) -> str | None:
        """Download and upload a thumbnail image to Builder.io."""
        if not thumbnail_url:
            return None

        local_path = self.download_image(thumbnail_url)
        if not local_path:
            return None

        return self.upload_to_builder(local_path)
