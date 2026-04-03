"""
Builder.io client module for creating content entries via the Write API.

Supports two content models:
- 'blog-post': For blog articles, saved under /blog/<url_key>
- 'page': For static CMS pages, saved under /<url_key>
"""

import json
import logging
import time
import uuid

import requests

from .css_processor import process_html_for_builder

logger = logging.getLogger(__name__)


# Builder.io Write API rate limits (approximate):
# - 50 requests per 10 seconds for write operations
# - We add conservative delays to stay well within limits
WRITE_DELAY_SECONDS = 1.0
READ_DELAY_SECONDS = 0.3


class BuilderClient:
    """Client for Builder.io Content API to create and manage content entries."""

    BASE_URL = "https://builder.io/api/v1/write"
    CDN_BASE_URL = "https://cdn.builder.io/api/v3/content"

    def __init__(self, api_key: str, model_name: str = "blog-post",
                 blog_model: str = "blog-post", page_model: str = "page",
                 public_key: str = ""):
        self.api_key = api_key
        self.public_key = public_key
        self.model_name = model_name
        self.blog_model = blog_model
        self.page_model = page_model
        # Private keys start with "bpk-"; public keys don't
        self._is_private_key = api_key.startswith("bpk-")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        self._request_count = 0
        self._last_request_time = 0.0

    def _cdn_url(self, model: str, extra_params: str = "") -> str:
        """Build a CDN read URL.

        The CDN API (v3) always requires a public API key as a query param.
        Private keys (bpk-*) are only for the Write API (v1).
        If a public_key was provided, use it; otherwise fall back to the
        api_key (which works if it's already a public key).
        """
        read_key = self.public_key or self.api_key
        sep = "&" if extra_params else ""
        url = f"{self.CDN_BASE_URL}/{model}?apiKey={read_key}{sep}{extra_params}"
        return url

    def create_blog_entry(self, blog_data: dict, publish: bool = False, existing_entry_id: str = None) -> dict:
        """
        Create or update a blog article entry in Builder.io under the 'blog-post' model.

        If existing_entry_id is provided, the entry is updated instead of created,
        preventing duplicate pages when re-running migrations.

        Args:
            blog_data: Dict with keys: title, html_content, thumbnail, url_key,
                       meta_title, meta_description, categories, tags, published_at
            publish: Whether to publish immediately or save as draft
            existing_entry_id: If set, update this entry instead of creating a new one

        Returns:
            Dict with 'success' bool and 'data' or 'error'
        """
        url_key = blog_data.get("url_key", "")
        url_path = f"/blog/{url_key}"

        # Convert tags to Builder.io format
        raw_tags = blog_data.get("tags", [])
        builder_tags = []
        for t in raw_tags:
            if isinstance(t, dict):
                builder_tags.append(t)
            elif isinstance(t, str) and t:
                builder_tags.append({"tag": t})

        # Convert HTML content to Builder.io blocks format
        blocks = self._html_to_builder_blocks(blog_data.get("html_content", ""))

        entry = {
            "name": blog_data.get("title", "Untitled"),
            "published": "published" if publish else "draft",
            "query": [
                {
                    "@type": "@builder.io/core:Query",
                    "property": "urlPath",
                    "operator": "is",
                    "value": url_path,
                }
            ],
            "data": {
                "title": blog_data.get("title", ""),
                "url": url_path,
                "slug": url_key,
                "description": blog_data.get("meta_description", "") or blog_data.get("title", ""),
                "excerpt": blog_data.get("meta_description", "") or blog_data.get("title", ""),
                "coverImage": blog_data.get("thumbnail", ""),
                "coverImageAlt": blog_data.get("thumbnail_alt", "") or blog_data.get("title", ""),
                "publishDate": blog_data.get("published_at", "") or self._current_iso_date(),
                "authorName": blog_data.get("author", ""),
                "canonicalUrl": blog_data.get("canonical_url", ""),
                "tags": builder_tags,
                "noindex": False,
                "nofollow": False,
                "isFeatured": False,
                "blocks": blocks,
            },
        }

        # Preserve meta fields
        if blog_data.get("meta_title"):
            entry["data"]["metaTitle"] = blog_data["meta_title"]
        if blog_data.get("meta_description"):
            entry["data"]["metaDescription"] = blog_data["meta_description"]

        if existing_entry_id:
            return self.update_entry(existing_entry_id, entry, model_override=self.blog_model)
        return self._create_content(entry, model_override=self.blog_model)

    def create_static_page_entry(self, page_data: dict, publish: bool = False, existing_entry_id: str = None) -> dict:
        """
        Create a new static page entry in Builder.io under the 'page' model.

        Args:
            page_data: Dict with keys: title, html_content, url_key,
                       meta_title, meta_description
            publish: Whether to publish immediately or save as draft

        Returns:
            Dict with 'success' bool and 'data' or 'error'
        """
        url_key = page_data.get("url_key", "")
        # Static pages go directly under root path
        url_path = f"/{url_key}" if url_key else "/"

        blocks = self._html_to_builder_blocks(page_data.get("html_content", ""))

        entry = {
            "name": page_data.get("title", "Untitled"),
            "published": "published" if publish else "draft",
            "query": [
                {
                    "@type": "@builder.io/core:Query",
                    "property": "urlPath",
                    "operator": "is",
                    "value": url_path,
                }
            ],
            "data": {
                "title": page_data.get("title", ""),
                "url": url_path,
                "slug": url_key,
                "blocks": blocks,
            },
        }

        # Preserve meta fields
        if page_data.get("meta_title"):
            entry["data"]["metaTitle"] = page_data["meta_title"]
        if page_data.get("meta_description"):
            entry["data"]["metaDescription"] = page_data["meta_description"]
        if page_data.get("meta_keywords"):
            entry["data"]["metaKeywords"] = page_data["meta_keywords"]
        if page_data.get("thumbnail"):
            entry["data"]["coverImage"] = page_data["thumbnail"]

        if existing_entry_id:
            return self.update_entry(existing_entry_id, entry, model_override=self.page_model)
        return self._create_content(entry, model_override=self.page_model)

    def create_entry(self, page_data: dict, page_type: str = "blog", publish: bool = False, existing_entry_id: str = None) -> dict:
        """
        Unified entry creation/update - routes to blog or static page based on page_type.

        If existing_entry_id is provided, updates the existing entry instead of
        creating a duplicate.

        Args:
            page_data: Page content dict
            page_type: 'blog' or 'static'
            publish: Whether to publish immediately
            existing_entry_id: If set, update this entry instead of creating a new one
        """
        if page_type == "static":
            return self.create_static_page_entry(page_data, publish, existing_entry_id=existing_entry_id)
        else:
            return self.create_blog_entry(page_data, publish, existing_entry_id=existing_entry_id)

    def _create_content(self, entry: dict, model_override: str = None) -> dict:
        """Send the content creation request to Builder.io with rate limiting."""
        model = model_override or self.model_name
        url = f"{self.BASE_URL}/{model}"

        # Rate limiting
        self._rate_limit(WRITE_DELAY_SECONDS)

        try:
            response = self.session.post(url, json=entry, timeout=60)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 10))
                logger.warning(f"Rate limited by Builder.io, waiting {retry_after}s...")
                time.sleep(retry_after)
                response = self.session.post(url, json=entry, timeout=60)

            response.raise_for_status()
            result = response.json()
            logger.info(f"Created Builder.io entry: {entry['name']} (model: {model})")
            return {"success": True, "data": result}

        except requests.exceptions.HTTPError as e:
            error_body = ""
            try:
                error_body = e.response.text
            except Exception:
                pass
            logger.error(f"Builder.io API error: {e} - {error_body}")
            return {"success": False, "error": str(e), "details": error_body}

        except Exception as e:
            logger.error(f"Failed to create Builder.io entry: {e}")
            return {"success": False, "error": str(e)}

    def _rate_limit(self, min_delay: float):
        """Enforce minimum delay between API requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < min_delay:
            sleep_time = min_delay - elapsed
            logger.debug(f"Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)
        self._last_request_time = time.time()
        self._request_count += 1

    def _html_to_builder_blocks(self, html_content: str) -> list[dict]:
        """
        Convert HTML content to Builder.io block format.

        Creates a Section > Custom Code structure that preserves the original
        layout while still being editable in Builder.io's visual editor.
        Users can drag/drop additional blocks around the migrated content,
        and edit text within Custom Code blocks.
        """
        if not html_content:
            return []

        # Process HTML to fix Magento Page Builder CSS and add base styles
        html_content = process_html_for_builder(html_content)

        blocks = [
            {
                "@type": "@builder.io/sdk:Element",
                "@version": 2,
                "id": f"builder-{uuid.uuid4().hex[:24]}",
                "component": {
                    "name": "Core:Section",
                    "options": {
                        "maxWidth": 900,
                        "lazyLoad": False,
                    },
                },
                "children": [
                    {
                        "@type": "@builder.io/sdk:Element",
                        "@version": 2,
                        "id": f"builder-{uuid.uuid4().hex[:24]}",
                        "component": {
                            "name": "Custom Code",
                            "options": {
                                "code": html_content,
                            },
                        },
                        "responsiveStyles": {
                            "large": {
                                "display": "flex",
                                "flexDirection": "column",
                                "position": "relative",
                                "flexShrink": "0",
                                "boxSizing": "border-box",
                                "marginTop": "20px",
                            }
                        },
                    }
                ],
                "responsiveStyles": {
                    "large": {
                        "display": "flex",
                        "flexDirection": "column",
                        "position": "relative",
                        "flexShrink": "0",
                        "boxSizing": "border-box",
                        "marginTop": "0px",
                        "paddingLeft": "20px",
                        "paddingRight": "20px",
                        "paddingTop": "20px",
                        "paddingBottom": "20px",
                        "minHeight": "100px",
                    }
                },
            }
        ]

        return blocks

    def get_preview_url(self, entry_id: str, model_override: str = None) -> str | None:
        """Build a Builder.io preview URL for visual verification.

        Returns a CDN URL that renders the entry's content with preview=true,
        or None if no public key is configured.
        """
        read_key = self.public_key or (self.api_key if not self._is_private_key else "")
        if not read_key:
            return None
        model = model_override or self.model_name
        return (
            f"{self.CDN_BASE_URL}/{model}/{entry_id}"
            f"?apiKey={read_key}&preview=true&includeUnpublished=true"
        )

    def _current_iso_date(self) -> str:
        """Return current datetime in ISO format."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def publish_entry(self, entry_id: str, model_override: str = None) -> dict:
        """
        Publish a single draft entry by its Builder.io ID.

        Uses the Write API: PUT /api/v1/write/<model>/<id>
        """
        model = model_override or self.model_name
        url = f"{self.BASE_URL}/{model}/{entry_id}"

        self._rate_limit(WRITE_DELAY_SECONDS)

        try:
            response = self.session.put(
                url,
                json={"published": "published"},
                timeout=60,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 10))
                logger.warning(f"Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                response = self.session.put(
                    url,
                    json={"published": "published"},
                    timeout=60,
                )

            response.raise_for_status()
            result = response.json()
            logger.info(f"Published entry {entry_id} (model: {model})")
            return {"success": True, "data": result}

        except requests.exceptions.HTTPError as e:
            error_body = ""
            try:
                error_body = e.response.text
            except Exception:
                pass
            logger.error(f"Failed to publish entry {entry_id}: {e} - {error_body}")
            return {"success": False, "error": str(e), "details": error_body}

        except Exception as e:
            logger.error(f"Failed to publish entry {entry_id}: {e}")
            return {"success": False, "error": str(e)}

    def unpublish_entry(self, entry_id: str, model_override: str = None) -> dict:
        """Unpublish (set to draft) a single entry by its Builder.io ID."""
        model = model_override or self.model_name
        url = f"{self.BASE_URL}/{model}/{entry_id}"

        self._rate_limit(WRITE_DELAY_SECONDS)

        try:
            response = self.session.put(
                url,
                json={"published": "draft"},
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()
            logger.info(f"Unpublished entry {entry_id} (model: {model})")
            return {"success": True, "data": result}
        except Exception as e:
            logger.error(f"Failed to unpublish entry {entry_id}: {e}")
            return {"success": False, "error": str(e)}

    def fetch_draft_entries(self, limit: int = 100, model_override: str = None) -> list[dict]:
        """Fetch all draft (unpublished) entries."""
        model = model_override or self.model_name
        self._rate_limit(READ_DELAY_SECONDS)

        params = f"limit={limit}&includeUnpublished=true&query.published.$ne=published"
        url = self._cdn_url(model, params)
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            # Filter to drafts only (API may not perfectly filter)
            drafts = [e for e in results if e.get("published") != "published"]
            return drafts
        except Exception as e:
            logger.error(f"Failed to fetch draft entries: {e}")
            return []

    def publish_all_drafts(self, model_override: str = None, progress_callback=None) -> dict:
        """
        Publish all draft entries for a given model.

        Args:
            model_override: Model name (defaults to self.model_name)
            progress_callback: Optional fn(current, total, entry_name)

        Returns:
            Dict with 'total', 'published', 'failed', 'details'
        """
        model = model_override or self.model_name
        drafts = self.fetch_draft_entries(limit=200, model_override=model)

        results = {
            "total": len(drafts),
            "published": 0,
            "failed": 0,
            "details": [],
        }

        logger.info(f"Found {len(drafts)} draft entries in model '{model}'")

        for i, entry in enumerate(drafts, 1):
            entry_id = entry.get("id", "")
            entry_name = entry.get("name", "Untitled")
            slug = entry.get("data", {}).get("slug", "")

            if progress_callback:
                progress_callback(i, len(drafts), entry_name)

            pub_result = self.publish_entry(entry_id, model_override=model)

            detail = {
                "id": entry_id,
                "name": entry_name,
                "slug": slug,
                "success": pub_result.get("success", False),
                "error": pub_result.get("error", ""),
            }
            results["details"].append(detail)

            if pub_result.get("success"):
                results["published"] += 1
                logger.info(f"  [{i}/{len(drafts)}] Published: {entry_name}")
            else:
                results["failed"] += 1
                logger.error(f"  [{i}/{len(drafts)}] Failed: {entry_name} - {pub_result.get('error', '')}")

        return results

    def check_entry_exists(self, url_key: str, model_override: str = None) -> dict | None:
        """Check if an entry with this URL key already exists.

        Returns:
            The existing entry dict (with 'id') if found, or None.
            Truthy when entry exists, falsy when it doesn't — backward compatible
            with code that used the old boolean return value.
        """
        model = model_override or self.model_name
        self._rate_limit(READ_DELAY_SECONDS)

        params = f"query.data.slug={url_key}&limit=1&fields=id,name,data.slug&includeUnpublished=true"
        check_url = self._cdn_url(model, params)
        try:
            response = self.session.get(check_url, timeout=15)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return results[0] if results else None
        except Exception as e:
            logger.warning(f"Could not check for existing entry {url_key}: {e}")
            return None

    def update_entry(self, entry_id: str, entry: dict, model_override: str = None) -> dict:
        """Update an existing Builder.io content entry by ID."""
        model = model_override or self.model_name
        url = f"{self.BASE_URL}/{model}/{entry_id}"

        self._rate_limit(WRITE_DELAY_SECONDS)

        try:
            response = self.session.put(url, json=entry, timeout=60)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 10))
                logger.warning(f"Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                response = self.session.put(url, json=entry, timeout=60)

            response.raise_for_status()
            result = response.json()
            logger.info(f"Updated Builder.io entry: {entry.get('name', entry_id)} (model: {model})")
            return {"success": True, "data": result}

        except requests.exceptions.HTTPError as e:
            error_body = ""
            try:
                error_body = e.response.text
            except Exception:
                pass
            logger.error(f"Builder.io API error on update: {e} - {error_body}")
            return {"success": False, "error": str(e), "details": error_body}

        except Exception as e:
            logger.error(f"Failed to update Builder.io entry: {e}")
            return {"success": False, "error": str(e)}

    def list_entries(self, limit: int = 25, offset: int = 0, model_override: str = None) -> list[dict]:
        """List existing entries in Builder.io."""
        model = model_override or self.model_name
        self._rate_limit(READ_DELAY_SECONDS)

        params = f"limit={limit}&offset={offset}&fields=id,name,data.slug,data.title,published"
        url = self._cdn_url(model, params)
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"Failed to list entries: {e}")
            return []

    def fetch_entry_full(self, slug: str = None, include_unpublished: bool = True, model_override: str = None) -> dict | None:
        """Fetch a full entry by slug."""
        model = model_override or self.model_name
        self._rate_limit(READ_DELAY_SECONDS)

        params = "limit=1"
        if slug:
            params += f"&query.data.slug={slug}"
        if include_unpublished:
            params += "&includeUnpublished=true"

        url = self._cdn_url(model, params)
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Failed to fetch entry {slug}: {e}")
            return None

    def fetch_all_entries(self, limit: int = 100, include_unpublished: bool = True, model_override: str = None) -> list[dict]:
        """Fetch all entries with full data."""
        model = model_override or self.model_name
        self._rate_limit(READ_DELAY_SECONDS)

        params = f"limit={limit}"
        if include_unpublished:
            params += "&includeUnpublished=true"

        url = self._cdn_url(model, params)
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"Failed to fetch entries: {e}")
            return []
