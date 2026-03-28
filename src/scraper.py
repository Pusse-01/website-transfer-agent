"""
Web page scraper module for Magento websites.
Supports:
- Blog posts via Amasty Blog GraphQL API + HTML fallback
- Static CMS pages via Magento cmsPage GraphQL + HTML fallback
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
                    tags {
                        name
                        url_key
                        tag_id
                    }
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
        queries = [
            # Attempt 1: full fields with tags as object type (AmBlogTags)
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
                    tags {
                        name
                        url_key
                        tag_id
                    }
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
            # Attempt 2: minimal safe fields (no tags/categories)
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
                    continue

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
        thumbnail = post_data.get("post_thumbnail") or post_data.get("list_thumbnail") or ""
        if thumbnail and not thumbnail.startswith("http"):
            thumbnail = urljoin(self.base_url, thumbnail)

        html_content = post_data.get("full_content") or post_data.get("short_content") or ""

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
            "page_type": "blog",
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
        result = self._parse_html_post(soup, url_key, url)
        result["page_type"] = "blog"
        return result

    def _parse_html_post(self, soup: BeautifulSoup, url_key: str, url: str) -> dict:
        """Parse blog post from HTML page."""
        title = ""
        for selector in ["h1.page-title", "h1.post-title", "h1.amblog-title", ".amblog-post-title", "h1"]:
            el = soup.select_one(selector)
            if el:
                title = el.get_text(strip=True)
                break

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

        if not html_content:
            main = soup.select_one("main") or soup.select_one("#maincontent")
            if main:
                html_content = str(main)

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

        meta_desc = ""
        meta_el = soup.select_one('meta[name="description"]')
        if meta_el:
            meta_desc = meta_el.get("content", "")

        meta_title = ""
        if soup.title:
            meta_title = soup.title.string or ""

        return {
            "title": title,
            "html_content": html_content,
            "thumbnail": thumbnail,
            "meta_title": meta_title,
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

        return list(dict.fromkeys(images))

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
                        path = urlparse(loc_text).path
                        key = path.replace(self.blog_path, "").strip("/")
                        if key:
                            url_keys.append(key)

                if url_keys:
                    break
            except Exception as e:
                logger.warning(f"Could not fetch sitemap {sitemap_url}: {e}")

        return url_keys


class StaticPageScraper:
    """Scrapes static CMS pages from a Magento website."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; PageMigrationAgent/1.0)",
            "Accept": "text/html,application/json",
        })
        self.graphql_url = f"{self.base_url}/graphql"

    def fetch_page_by_url(self, page_url: str, url_key: str = "") -> dict:
        """
        Fetch a static page. Tries CMS GraphQL first, then falls back to HTML scraping.

        Args:
            page_url: Full URL of the page to scrape
            url_key: URL key/identifier for this page
        """
        logger.info(f"Fetching static page: {url_key or page_url}")

        # Try GraphQL cmsPage first using the url_key
        if url_key:
            result = self._fetch_via_cms_graphql(url_key)
            if result and not result.get("error"):
                return result

        # Fallback to HTML scraping
        logger.info(f"GraphQL failed for {url_key}, falling back to HTML scraping")
        return self._fetch_via_html(page_url, url_key)

    def _fetch_via_cms_graphql(self, identifier: str) -> dict | None:
        """Fetch CMS page via Magento's built-in cmsPage GraphQL query."""
        query = """
        query GetCmsPage($identifier: String!) {
            cmsPage(identifier: $identifier) {
                identifier
                url_key
                title
                content
                content_heading
                page_layout
                meta_title
                meta_description
                meta_keywords
            }
        }
        """
        try:
            response = self.session.post(
                self.graphql_url,
                json={"query": query, "variables": {"identifier": identifier}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.warning(f"CMS GraphQL errors for {identifier}: {data['errors']}")
                return None

            page_data = data.get("data", {}).get("cmsPage")
            if not page_data:
                return None

            html_content = page_data.get("content", "") or ""

            return {
                "title": page_data.get("title", ""),
                "html_content": html_content,
                "content_heading": page_data.get("content_heading", ""),
                "thumbnail": "",
                "meta_title": page_data.get("meta_title", "") or page_data.get("title", ""),
                "meta_description": page_data.get("meta_description", ""),
                "meta_keywords": page_data.get("meta_keywords", ""),
                "url_key": page_data.get("identifier", "") or page_data.get("url_key", "") or identifier,
                "page_layout": page_data.get("page_layout", ""),
                "categories": [],
                "tags": [],
                "published_at": "",
                "images": self._extract_images_from_html(html_content),
                "source": "cms_graphql",
                "page_type": "static",
            }

        except Exception as e:
            logger.warning(f"CMS GraphQL request failed for {identifier}: {e}")
            return None

    def _fetch_via_html(self, page_url: str, url_key: str = "") -> dict:
        """Fetch static page by scraping its HTML."""
        if not page_url:
            return {"error": "No URL provided", "url_key": url_key}

        try:
            response = self.session.get(page_url, timeout=30)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch HTML for {page_url}: {e}")
            return {"error": str(e), "url_key": url_key}

        soup = BeautifulSoup(response.text, "html.parser")
        return self._parse_static_page(soup, url_key, page_url)

    def _parse_static_page(self, soup: BeautifulSoup, url_key: str, url: str) -> dict:
        """Parse a static CMS page from HTML."""
        # Title
        title = ""
        for selector in ["h1.page-title span", "h1.page-title", ".page-title-wrapper h1", "h1"]:
            el = soup.select_one(selector)
            if el:
                title = el.get_text(strip=True)
                break

        # Main content area - try CMS-specific selectors first
        html_content = ""
        for selector in [
            ".cms-page-view .column.main",
            ".cms-content",
            ".page-main .column.main",
            "#maincontent .column.main",
            ".page-main",
            "#maincontent",
            "main",
        ]:
            el = soup.select_one(selector)
            if el:
                # Remove navigation, breadcrumbs, sidebar elements
                for unwanted in el.select(
                    ".breadcrumbs, .sidebar, nav, .nav, header, footer, "
                    ".page-title-wrapper, script, .modal-popup"
                ):
                    unwanted.decompose()
                html_content = str(el)
                break

        # Extract thumbnail / hero image
        thumbnail = ""
        og_image = soup.select_one('meta[property="og:image"]')
        if og_image:
            thumbnail = og_image.get("content", "")

        # Meta description
        meta_desc = ""
        meta_el = soup.select_one('meta[name="description"]')
        if meta_el:
            meta_desc = meta_el.get("content", "")

        # Meta title
        meta_title = ""
        if soup.title:
            meta_title = soup.title.string or ""

        # Meta keywords
        meta_keywords = ""
        kw_el = soup.select_one('meta[name="keywords"]')
        if kw_el:
            meta_keywords = kw_el.get("content", "")

        return {
            "title": title,
            "html_content": html_content,
            "thumbnail": thumbnail,
            "meta_title": meta_title,
            "meta_description": meta_desc,
            "meta_keywords": meta_keywords,
            "url_key": url_key,
            "categories": [],
            "tags": [],
            "published_at": "",
            "images": self._extract_images_from_html(html_content),
            "source": "html",
            "page_type": "static",
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

        return list(dict.fromkeys(images))
