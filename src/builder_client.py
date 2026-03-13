"""
Builder.io client module for creating blog content entries via the Write API.
"""

import json
import logging
import time
import uuid

import requests

logger = logging.getLogger(__name__)


class BuilderClient:
    """Client for Builder.io Content API to create and manage blog articles."""

    BASE_URL = "https://builder.io/api/v1/write"

    def __init__(self, api_key: str, model_name: str = "blog-article"):
        self.api_key = api_key
        self.model_name = model_name
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def create_blog_entry(self, blog_data: dict, publish: bool = False) -> dict:
        """
        Create a new blog article entry in Builder.io.

        Args:
            blog_data: Dict with keys: title, html_content, thumbnail, url_key,
                       meta_title, meta_description, categories, tags, published_at
            publish: Whether to publish immediately or save as draft

        Returns:
            Builder.io API response dict
        """
        url_key = blog_data.get("url_key", "")
        url_path = f"/blog/{url_key}"

        # Convert tags to Builder.io format: [{"tag": "value"}, ...]
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

        return self._create_content(entry)

    def create_blog_entry_with_custom_fields(
        self, blog_data: dict, custom_fields: dict = None, publish: bool = False
    ) -> dict:
        """
        Create a blog entry with additional custom fields.
        Useful when the Builder.io model has custom field definitions.
        """
        url_key = blog_data.get("url_key", "")
        url_path = f"/blog/{url_key}"

        raw_tags = blog_data.get("tags", [])
        builder_tags = []
        for t in raw_tags:
            if isinstance(t, dict):
                builder_tags.append(t)
            elif isinstance(t, str) and t:
                builder_tags.append({"tag": t})

        blocks = self._html_to_builder_blocks(blog_data.get("html_content", ""))

        data_fields = {
            "title": blog_data.get("title", ""),
            "url": url_path,
            "slug": url_key,
            "description": blog_data.get("meta_description", "") or blog_data.get("title", ""),
            "excerpt": blog_data.get("meta_description", "") or blog_data.get("title", ""),
            "coverImage": blog_data.get("thumbnail", ""),
            "coverImageAlt": blog_data.get("thumbnail_alt", "") or blog_data.get("title", ""),
            "publishDate": blog_data.get("published_at", "") or self._current_iso_date(),
            "tags": builder_tags,
            "blocks": blocks,
        }

        if custom_fields:
            data_fields.update(custom_fields)

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
            "data": data_fields,
        }

        return self._create_content(entry)

    def _create_content(self, entry: dict) -> dict:
        """Send the content creation request to Builder.io."""
        url = f"{self.BASE_URL}/{self.model_name}"

        try:
            response = self.session.post(url, json=entry, timeout=60)

            if response.status_code == 429:
                # Rate limited - wait and retry
                retry_after = int(response.headers.get("Retry-After", 5))
                logger.warning(f"Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                response = self.session.post(url, json=entry, timeout=60)

            response.raise_for_status()
            result = response.json()
            logger.info(f"Created Builder.io entry: {entry['name']}")
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

    def _html_to_builder_blocks(self, html_content: str) -> list[dict]:
        """
        Convert HTML content to Builder.io block format.

        Uses a Custom Code block wrapping to preserve the original blog
        formatting, inside a Section block for proper layout.
        """
        if not html_content:
            return []

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

    def _current_iso_date(self) -> str:
        """Return current datetime in ISO format."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def check_entry_exists(self, url_key: str) -> bool:
        """Check if a blog entry with this URL key already exists."""
        check_url = (
            f"https://cdn.builder.io/api/v3/content/{self.model_name}"
            f"?apiKey={self.api_key}"
            f"&query.data.slug={url_key}"
            f"&limit=1"
            f"&fields=id,name"
        )
        try:
            response = self.session.get(check_url, timeout=15)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return len(results) > 0
        except Exception as e:
            logger.warning(f"Could not check for existing entry {url_key}: {e}")
            return False

    def list_entries(self, limit: int = 25, offset: int = 0) -> list[dict]:
        """List existing blog entries in Builder.io."""
        url = (
            f"https://cdn.builder.io/api/v3/content/{self.model_name}"
            f"?apiKey={self.api_key}"
            f"&limit={limit}"
            f"&offset={offset}"
            f"&fields=id,name,data.slug,data.title,published"
        )
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"Failed to list entries: {e}")
            return []

    def fetch_entry_full(self, slug: str = None, include_unpublished: bool = True) -> dict | None:
        """Fetch a full entry by slug, including blocks and all data fields."""
        params = f"apiKey={self.api_key}&limit=1"
        if slug:
            params += f"&query.data.slug={slug}"
        if include_unpublished:
            params += "&includeUnpublished=true"

        url = f"https://cdn.builder.io/api/v3/content/{self.model_name}?{params}"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Failed to fetch entry {slug}: {e}")
            return None

    def fetch_all_entries(self, limit: int = 100, include_unpublished: bool = True) -> list[dict]:
        """Fetch all entries with full data."""
        params = f"apiKey={self.api_key}&limit={limit}"
        if include_unpublished:
            params += "&includeUnpublished=true"

        url = f"https://cdn.builder.io/api/v3/content/{self.model_name}?{params}"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except Exception as e:
            logger.error(f"Failed to fetch entries: {e}")
            return []
