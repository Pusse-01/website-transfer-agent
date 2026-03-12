"""
Blog scraper module for Magento/Amasty Blog websites.
Supports both GraphQL API and HTML fallback for extracting blog content.
"""

import json
import re
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class BlogScraper:
    """Scrapes blog content from a Magento website with Amasty Blog."""

    def __init__(self, base_url: str, blog_path: str = "/blog/"):
        self.base_url = base_url.rstrip("/")
        self.blog_path = blog_path
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; BlogMigrationAgent/1.0)",
            "Accept": "text/html,application/json",
        })
        self.graphql_url = f"{self.base_url}/graphql"

    def fetch_post_by_url_key(self, url_key: str) -> dict:
        """Fetch a single blog post by its URL key. Tries GraphQL first, falls back to HTML."""
        logger.info(f"Fetching blog post: {url_key}")

        # Try GraphQL first
        post = self._fetch_via_graphql(url_key)
        if post:
            return post

        # Fallback to HTML scraping
        logger.info(f"GraphQL failed for {url_key}, falling back to HTML scraping")
        return self._fetch_via_html(url_key)

    def fetch_all_posts_via_graphql(self, page: int = 1) -> list[dict]:
        """Fetch paginated blog post listing via GraphQL."""
        query = """
        query GetBlogPosts($page: Int!) {
            amBlogPosts(type: ALL, page: $page) {
                all_post_size
                items {
                    post_id
                    title
                    url_key
                    short_content
                    post_thumbnail
                    list_thumbnail
                    published_at
                    categories
                    tags
                    tag_ids
                }
            }
        }
        """
        try:
            response = self.session.post(
                self.graphql_url,
                json={"query": query, "variables": {"page": page}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.warning(f"GraphQL listing errors: {data['errors']}")
                return []

            posts_data = data.get("data", {}).get("amBlogPosts", {})
            total = posts_data.get("all_post_size", 0)
            items = posts_data.get("items", [])
            logger.info(f"Fetched page {page}: {len(items)} posts (total: {total})")
            return items

        except Exception as e:
            logger.warning(f"GraphQL listing failed: {e}")
            return []

    def _fetch_via_graphql(self, url_key: str) -> dict | None:
        """Fetch blog post via Magento GraphQL API (Amasty Blog).

        Uses a progressive query strategy: starts with all fields, then
        retries with fewer fields if the API rejects unknown ones.
        """
        # Full query - try first
        queries = [
            # Attempt 1: tags/categories as scalar fields
            """
            query GetBlogPost($urlKey: String!) {
                amBlogPost(urlKey: $urlKey) {
                    post_id
                    title
                    full_content
                    short_content
                    post_thumbnail
                    post_thumbnail_alt
                    list_thumbnail
                    list_thumbnail_alt
                    meta_title
                    meta_description
                    meta_tags
                    categories
                    tags
                    tag_ids
                    url_key
                    published_at
                    created_at
                    updated_at
                    status
                    author_id
                    views
                    is_featured
                }
            }
            """,
            # Attempt 2: minimal safe fields only
            """
            query GetBlogPost($urlKey: String!) {
                amBlogPost(urlKey: $urlKey) {
                    post_id
                    title
                    full_content
                    short_content
                    post_thumbnail
                    list_thumbnail
                    meta_title
                    meta_description
                    url_key
                    published_at
                    status
                }
            }
            """,
        ]

        for i, query in enumerate(queries):
            try:
                response = self.session.post(
                    self.graphql_url,
                    json={"query": query, "variables": {"urlKey": url_key}},
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()

                if "errors" in data:
                    logger.warning(f"GraphQL attempt {i+1} failed for {url_key}: {data['errors']}")
                    continue  # Try next query variant

                post_data = data.get("data", {}).get("amBlogPost")
                if not post_data:
                    return None

                return self._normalize_graphql_post(post_data)

            except Exception as e:
                logger.warning(f"GraphQL request failed for {url_key}: {e}")
                return None

        return None

    def _normalize_graphql_post(self, post_data: dict) -> dict:
        """Normalize GraphQL response into a standard format."""
        # Resolve image URLs
        thumbnail = post_data.get("post_thumbnail") or post_data.get("list_thumbnail") or ""
        if thumbnail and not thumbnail.startswith("http"):
            thumbnail = urljoin(self.base_url, thumbnail)

        html_content = post_data.get("full_content") or post_data.get("short_content") or ""

        # Tags may come as a list of strings, list of objects, or a single string
        raw_tags = post_data.get("tags") or []
        if isinstance(raw_tags, str):
            tags = [raw_tags] if raw_tags else []
        elif isinstance(raw_tags, list) and raw_tags:
            if isinstance(raw_tags[0], dict):
                tags = [t.get("name", "") for t in raw_tags if t.get("name")]
            else:
                tags = [str(t) for t in raw_tags if t]
        else:
            tags = []

        return {
            "title": post_data.get("title", ""),
            "html_content": html_content,
            "thumbnail": thumbnail,
            "thumbnail_alt": post_data.get("post_thumbnail_alt", ""),
            "meta_title": post_data.get("meta_title", ""),
            "meta_description": post_data.get("meta_description", ""),
            "categories": post_data.get("categories", []),
            "tags": tags,
            "url_key": post_data.get("url_key", ""),
            "published_at": post_data.get("published_at", ""),
            "created_at": post_data.get("created_at", ""),
            "updated_at": post_data.get("updated_at", ""),
            "images": self._extract_images_from_html(html_content),
            "source": "graphql",
        }

    def _fetch_via_html(self, url_key: str) -> dict:
        """Fetch blog post by scraping the HTML page."""
        url = f"{self.base_url}{self.blog_path}{url_key}"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch HTML for {url_key}: {e}")
            return {"error": str(e), "url_key": url_key}

        soup = BeautifulSoup(response.text, "html.parser")
        return self._parse_html_post(soup, url_key, url)

    def _parse_html_post(self, soup: BeautifulSoup, url_key: str, url: str) -> dict:
        """Parse blog post from HTML page."""
        # Try multiple selectors for the title
        title = ""
        for selector in ["h1.page-title", "h1.post-title", "h1.amblog-title", ".amblog-post-title", "h1"]:
            el = soup.select_one(selector)
            if el:
                title = el.get_text(strip=True)
                break

        # Try multiple selectors for the content body
        html_content = ""
        for selector in [
            ".amblog-post-content",
            ".amblog-content",
            ".post-content",
            ".blog-post-content",
            "article .content",
            ".entry-content",
            "article",
        ]:
            el = soup.select_one(selector)
            if el:
                html_content = str(el)
                break

        # If no content found, try getting the main content area
        if not html_content:
            main = soup.select_one("main") or soup.select_one("#maincontent")
            if main:
                html_content = str(main)

        # Extract thumbnail/featured image
        thumbnail = ""
        for selector in [
            ".amblog-post-image img",
            ".post-thumbnail img",
            'meta[property="og:image"]',
            ".amblog-element-post img",
        ]:
            el = soup.select_one(selector)
            if el:
                thumbnail = el.get("src") or el.get("content", "")
                if thumbnail and not thumbnail.startswith("http"):
                    thumbnail = urljoin(url, thumbnail)
                break

        # Extract meta description
        meta_desc = ""
        meta_el = soup.select_one('meta[name="description"]')
        if meta_el:
            meta_desc = meta_el.get("content", "")

        return {
            "title": title,
            "html_content": html_content,
            "thumbnail": thumbnail,
            "meta_title": soup.title.string if soup.title else title,
            "meta_description": meta_desc,
            "categories": [],
            "tags": [],
            "url_key": url_key,
            "published_at": "",
            "images": self._extract_images_from_html(html_content),
            "source": "html",
        }

    def _extract_images_from_html(self, html_content: str) -> list[str]:
        """Extract all image URLs from HTML content."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, "html.parser")
        images = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src:
                if not src.startswith("http"):
                    src = urljoin(self.base_url, src)
                images.append(src)

        return list(dict.fromkeys(images))  # deduplicate preserving order

    def fetch_blog_list_from_sitemap(self) -> list[str]:
        """Try to get blog URL keys from sitemap."""
        sitemap_urls = [
            f"{self.base_url}/sitemap.xml",
            f"{self.base_url}/pub/sitemap/sitemap.xml",
        ]
        url_keys = []

        for sitemap_url in sitemap_urls:
            try:
                response = self.session.get(sitemap_url, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "xml")

                for loc in soup.find_all("loc"):
                    loc_text = loc.get_text()
                    if self.blog_path in loc_text:
                        # Extract URL key from the blog URL
                        path = urlparse(loc_text).path
                        key = path.replace(self.blog_path, "").strip("/")
                        if key:
                            url_keys.append(key)

                if url_keys:
                    break
            except Exception as e:
                logger.warning(f"Could not fetch sitemap {sitemap_url}: {e}")

        return url_keys
