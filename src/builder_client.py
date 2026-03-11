"""
Builder.io client module for creating blog content entries via the Write API.
"""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class BuilderClient:
    """Client for Builder.io Content API to create and manage blog articles."""

    BASE_URL = "https://cdn.builder.io/api/v3/write"

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
        url_path = f"/blog/{blog_data.get('url_key', '')}"

        # Build the Builder.io content entry
        entry = {
            "name": blog_data.get("title", "Untitled"),
            "published": "published" if publish else "draft",
            "query": [
                {
                    "property": "urlPath",
                    "operator": "is",
                    "value": url_path,
                }
            ],
            "data": {
                "title": blog_data.get("title", ""),
                "url": url_path,
                "slug": blog_data.get("url_key", ""),
                "description": blog_data.get("meta_description", ""),
                "image": blog_data.get("thumbnail", ""),
                "author": blog_data.get("author", ""),
                "date": blog_data.get("published_at", ""),
                "categories": blog_data.get("categories", []),
                "tags": blog_data.get("tags", []),
                "blurb": blog_data.get("meta_description", ""),
                # The main blog content as Builder.io blocks
                "blocks": self._html_to_builder_blocks(
                    blog_data.get("html_content", "")
                ),
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
        url_path = f"/blog/{blog_data.get('url_key', '')}"

        data_fields = {
            "title": blog_data.get("title", ""),
            "url": url_path,
            "slug": blog_data.get("url_key", ""),
            "description": blog_data.get("meta_description", ""),
            "image": blog_data.get("thumbnail", ""),
            "date": blog_data.get("published_at", ""),
            "categories": blog_data.get("categories", []),
            "tags": blog_data.get("tags", []),
            "blocks": self._html_to_builder_blocks(
                blog_data.get("html_content", "")
            ),
        }

        if custom_fields:
            data_fields.update(custom_fields)

        entry = {
            "name": blog_data.get("title", "Untitled"),
            "published": "published" if publish else "draft",
            "query": [
                {
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

        Builder.io uses a block-based content structure. We wrap the HTML
        in a Custom HTML block to preserve the original formatting.
        """
        if not html_content:
            return []

        # Use a single Custom HTML block to preserve the original blog formatting
        # This ensures the content looks exactly like the source
        blocks = [
            {
                "@type": "@builder.io/sdk:Element",
                "component": {
                    "name": "Custom Code",
                    "options": {
                        "code": html_content,
                    },
                },
            }
        ]

        return blocks

    def check_entry_exists(self, url_key: str) -> bool:
        """Check if a blog entry with this URL key already exists."""
        url_path = f"/blog/{url_key}"
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
