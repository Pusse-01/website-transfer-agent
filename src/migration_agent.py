"""
Migration Agent - Orchestrates the full page migration pipeline.

Handles both blog posts and static CMS pages:
1. Read page list from Excel
2. Scrape content from source (GraphQL + HTML fallback)
3. Process images (download + re-upload to Builder.io)
4. Deduplicate content blocks & images (structural + visual)
5. Upload content to Builder.io (blog-post or page model)
6. Visual verification — screenshot comparison to catch remaining duplicates
7. Track status and export results

Every step is logged with the page_key for filtering.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .scraper import BlogScraper, StaticPageScraper, _is_soft_404_title
from .image_handler import ImageHandler
from .builder_client import BuilderClient
from .excel_reader import read_blog_list, read_static_page_list, detect_excel_type
from .migration_logger import MigrationLogger
from .visual_verifier import VisualVerifier, run_visual_verification
from .deduplication import deduplicate_content_blocks, deduplicate_similar_images
from .css_processor import process_html_for_builder
from .llm_layout_fixer import fix_layout as llm_fix_layout, is_enabled as llm_fix_enabled
from .live_capture import capture_live_fragment, is_available as live_capture_available

logger = logging.getLogger(__name__)

# Migration status constants
STATUS_PUBLISHED = "published_by_agent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_PENDING = "pending"
STATUS_CANCELLED = "cancelled"
STATUS_NEEDS_REVIEW = "needs_human_review"


def _normalize_url_key(url_key) -> str:
    """Strip '.html' and surrounding slashes from an Excel url_key.

    The source Magento platform serves CMS pages at '/foo.html'. Builder.io
    should host them at '/foo' — the migrated site is extension-free. This
    function is the single choke point that every pipeline branch runs url_keys
    through before building Builder paths or checking for existing entries.
    """
    if not url_key:
        return ""
    key = str(url_key).strip().strip("/")
    if key.lower().endswith(".html"):
        key = key[: -len(".html")]
    return key


class MigrationAgent:
    """Orchestrates page migration from source website to Builder.io."""

    def __init__(
        self,
        source_base_url: str,
        builder_api_key: str,
        builder_model: str = "blog-post",
        blog_model: str = "blog-post",
        page_model: str = "page",
        blog_path: str = "/news/",
        download_dir: str = "downloaded_images",
        run_id: str = None,
        builder_public_key: str = "",
        use_live_capture: bool = True,
        magento_login: dict | None = None,
    ):
        self.source_base_url = source_base_url
        self.blog_scraper = BlogScraper(source_base_url, blog_path)
        self.static_scraper = StaticPageScraper(source_base_url)
        self.image_handler = ImageHandler(builder_api_key, download_dir)
        self.builder = BuilderClient(
            builder_api_key, builder_model,
            blog_model=blog_model, page_model=page_model,
            public_key=builder_public_key,
        )
        self.blog_path = blog_path
        self.visual_verifier = VisualVerifier()
        # Live capture: renders URL in Playwright and pulls real stylesheets.
        # This is the ONLY reliable way to preserve carousels, sliders, and
        # hover states from the live Magento page.
        self.use_live_capture = use_live_capture and live_capture_available()
        # Optional Magento admin credentials. Not needed for public CMS pages
        # but passed through to the capture layer for non-public URLs.
        self.magento_login = magento_login

        # Logger
        self.mlog = MigrationLogger(run_id=run_id)

        # Migration state
        self.results = {
            "started_at": None,
            "completed_at": None,
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "needs_review": 0,
            "details": [],
        }

    def migrate_from_excel(
        self,
        excel_path: str,
        publish: bool = False,
        skip_existing: bool = True,
        limit: int = 0,
        priority_filter: int = 0,
        dry_run: bool = False,
        progress_callback=None,
    ) -> dict:
        """
        Migrate pages listed in an Excel file.
        Auto-detects whether it's a blog post list or static page list.

        Args:
            excel_path: Path to the Excel file
            publish: Whether to publish posts immediately
            skip_existing: Skip posts that already exist in Builder.io
            limit: Max number of posts to migrate (0 = all)
            priority_filter: Only migrate posts with priority <= this value (0 = all)
            dry_run: If True, scrape content but don't upload
            progress_callback: Optional callback fn(current, total, page_key, status_msg)
        """
        self.mlog.info("__pipeline__", "", "init", f"Starting migration from Excel: {excel_path}")

        excel_type = detect_excel_type(excel_path)
        self.mlog.info("__pipeline__", "", "excel_read", f"Detected Excel type: {excel_type}")

        if excel_type == "blog":
            return self._migrate_blog_from_excel(
                excel_path, publish, skip_existing, limit, priority_filter, dry_run, progress_callback
            )
        elif excel_type == "static":
            return self._migrate_static_from_excel(
                excel_path, publish, skip_existing, limit, dry_run, progress_callback
            )
        else:
            self.mlog.error("__pipeline__", "", "excel_read",
                            f"Could not detect Excel type. Please check the file format.")
            return self.results

    def _migrate_blog_from_excel(
        self, excel_path, publish, skip_existing, limit, priority_filter, dry_run, progress_callback
    ) -> dict:
        """Migrate blog posts from a Blog Post List Excel."""
        blog_posts = read_blog_list(excel_path)
        if not blog_posts:
            self.mlog.error("__pipeline__", "blog", "excel_read", "No published blog posts found in Excel file")
            return self.results

        self.mlog.info("__pipeline__", "blog", "excel_read",
                       f"Loaded {len(blog_posts)} published blog posts from Excel")

        # Apply priority filter
        if priority_filter > 0:
            blog_posts = [p for p in blog_posts if p.get("priority") and int(p["priority"]) <= priority_filter]

        # Apply limit
        if limit > 0:
            blog_posts = blog_posts[:limit]

        pages_to_migrate = []
        for post in blog_posts:
            clean_key = _normalize_url_key(post["url_key"])
            pages_to_migrate.append({
                "url_key": clean_key,
                "title": post.get("title", ""),
                "page_type": "blog",
                # primary_url keeps the original ``.html`` suffix (if any) so
                # the scraper still hits the live Magento page. The stripped
                # key is only used for Builder paths.
                "primary_url": f"{self.source_base_url}{self.blog_path}{post['url_key']}",
                "original_data": post,
            })

        return self._run_migration(pages_to_migrate, publish, skip_existing, dry_run, progress_callback)

    def _migrate_static_from_excel(
        self, excel_path, publish, skip_existing, limit, dry_run, progress_callback
    ) -> dict:
        """Migrate static pages from a Static Page List Excel."""
        static_pages = read_static_page_list(excel_path)
        if not static_pages:
            self.mlog.error("__pipeline__", "static", "excel_read", "No static pages found in Excel file")
            return self.results

        self.mlog.info("__pipeline__", "static", "excel_read",
                       f"Loaded {len(static_pages)} static pages from Excel")

        if limit > 0:
            static_pages = static_pages[:limit]

        pages_to_migrate = []
        for page in static_pages:
            pages_to_migrate.append({
                "url_key": _normalize_url_key(page["url_key"]),
                "title": page.get("title", ""),
                "page_type": "static",
                "primary_url": page.get("primary_url", ""),
                "original_data": page,
            })

        return self._run_migration(pages_to_migrate, publish, skip_existing, dry_run, progress_callback)

    def migrate_from_url_keys(
        self,
        url_keys: list[str],
        page_type: str = "blog",
        publish: bool = False,
        skip_existing: bool = True,
        dry_run: bool = False,
        progress_callback=None,
    ) -> dict:
        """Migrate specific pages by URL keys."""
        pages_to_migrate = []
        for url_key in url_keys:
            if page_type == "blog":
                primary_url = f"{self.source_base_url}{self.blog_path}{url_key}"
            else:
                primary_url = f"{self.source_base_url}/hk/zh/{url_key}"

            clean_key = _normalize_url_key(url_key)
            pages_to_migrate.append({
                "url_key": clean_key,
                "title": "",
                "page_type": page_type,
                "primary_url": primary_url,
                "original_data": {"url_key": clean_key},
            })

        return self._run_migration(pages_to_migrate, publish, skip_existing, dry_run, progress_callback)

    def migrate_combined(
        self,
        blog_excel_path: str = None,
        static_excel_path: str = None,
        publish: bool = False,
        skip_existing: bool = True,
        limit: int = 0,
        dry_run: bool = False,
        progress_callback=None,
    ) -> dict:
        """
        Migrate from both blog and static page Excel files in one run.
        This is the single-button-click entry point.
        """
        self.mlog.info("__pipeline__", "", "init", "Starting combined migration pipeline")

        pages_to_migrate = []

        # Load blog posts
        if blog_excel_path:
            blog_posts = read_blog_list(blog_excel_path)
            self.mlog.info("__pipeline__", "blog", "excel_read",
                           f"Loaded {len(blog_posts)} published blog posts")
            for post in blog_posts:
                pages_to_migrate.append({
                    "url_key": _normalize_url_key(post["url_key"]),
                    "title": post.get("title", ""),
                    "page_type": "blog",
                    "primary_url": f"{self.source_base_url}{self.blog_path}{post['url_key']}",
                    "original_data": post,
                })

        # Load static pages
        if static_excel_path:
            static_pages = read_static_page_list(static_excel_path)
            self.mlog.info("__pipeline__", "static", "excel_read",
                           f"Loaded {len(static_pages)} static pages")
            for page in static_pages:
                pages_to_migrate.append({
                    "url_key": _normalize_url_key(page["url_key"]),
                    "title": page.get("title", ""),
                    "page_type": "static",
                    "primary_url": page.get("primary_url", ""),
                    "original_data": page,
                })

        if limit > 0:
            pages_to_migrate = pages_to_migrate[:limit]

        if not pages_to_migrate:
            self.mlog.error("__pipeline__", "", "excel_read", "No pages found in either Excel file")
            return self.results

        return self._run_migration(pages_to_migrate, publish, skip_existing, dry_run, progress_callback)

    def _run_migration(
        self,
        pages: list[dict],
        publish: bool,
        skip_existing: bool,
        dry_run: bool,
        progress_callback=None,
    ) -> dict:
        """Core migration loop - process a list of pages."""
        self.results["started_at"] = datetime.now().isoformat()
        self.results["total"] = len(pages)

        self.mlog.info("__pipeline__", "", "init",
                       f"Starting migration of {len(pages)} pages (dry_run={dry_run})")

        for i, page_info in enumerate(pages, 1):
            url_key = page_info["url_key"]
            page_type = page_info["page_type"]
            primary_url = page_info["primary_url"]

            self.mlog.info(url_key, page_type, "init",
                           f"[{i}/{len(pages)}] Processing: {page_info.get('title', url_key)}")

            if progress_callback:
                progress_callback(i, len(pages), url_key, "Processing...")

            result = self._migrate_single_page(
                url_key=url_key,
                page_type=page_type,
                primary_url=primary_url,
                publish=publish,
                skip_existing=skip_existing,
                dry_run=dry_run,
            )

            # Attach original Excel data for export
            result["original_data"] = page_info.get("original_data", {})
            result["page_type"] = page_type
            self.results["details"].append(result)

            if result["status"] == STATUS_PUBLISHED:
                self.results["success"] += 1
            elif result["status"] == STATUS_SKIPPED:
                self.results["skipped"] += 1
            elif result["status"] == STATUS_NEEDS_REVIEW:
                self.results["needs_review"] += 1
            else:
                self.results["failed"] += 1

            if progress_callback:
                progress_callback(i, len(pages), url_key, result["status"])

            # Tiny yield between pages so the event loop stays responsive.
            # Actual rate limiting is handled per-request inside BuilderClient.
            if i < len(pages):
                time.sleep(0.05)

        self.results["completed_at"] = datetime.now().isoformat()
        self.mlog.info("__pipeline__", "", "complete", "Migration complete", {
            "total": self.results["total"],
            "success": self.results["success"],
            "failed": self.results["failed"],
            "skipped": self.results["skipped"],
            "needs_review": self.results["needs_review"],
        })

        return self.results

    def _migrate_single_page(
        self, url_key: str, page_type: str, primary_url: str,
        publish: bool, skip_existing: bool, dry_run: bool,
    ) -> dict:
        """Migrate a single page through the full pipeline."""
        result = {
            "url_key": url_key,
            "page_type": page_type,
            "status": STATUS_PENDING,
            "title": "",
            "error": None,
            "source": "",
            "images_processed": 0,
            "builder_id": "",
            "confidence": "high",
        }

        try:
            # Step 1: Check if already exists (for skip or upsert)
            existing_entry = None
            existing_entry_id = None
            if not dry_run:
                model = self.builder.blog_model if page_type == "blog" else self.builder.page_model
                existing_entry = self.builder.check_entry_exists(url_key, model_override=model)
                if existing_entry and skip_existing:
                    self.mlog.info(url_key, page_type, "upload",
                                   "Skipping - already exists in Builder.io")
                    result["status"] = STATUS_SKIPPED
                    return result
                existing_entry_id = existing_entry.get("id") if existing_entry else None

            # Step 2: Scrape the page
            self.mlog.info(url_key, page_type, "scrape", "Scraping content...")
            page_data = self._scrape_page(url_key, page_type, primary_url)

            if page_data.get("error"):
                self.mlog.error(url_key, page_type, "scrape",
                                f"Scraping failed: {page_data['error']}")
                result["status"] = STATUS_FAILED
                result["error"] = page_data["error"]
                return result

            if not page_data.get("html_content"):
                self.mlog.warning(url_key, page_type, "scrape",
                                  "No content found - flagging for human review")
                result["status"] = STATUS_NEEDS_REVIEW
                result["error"] = "No content found"
                result["confidence"] = "low"
                return result

            result["title"] = page_data.get("title", "")
            result["source"] = page_data.get("source", "")
            result["meta_title"] = page_data.get("meta_title", "")
            result["meta_description"] = page_data.get("meta_description", "")

            self.mlog.info(url_key, page_type, "scrape",
                           f"Scraped: {result['title']} (source: {result['source']})",
                           {"content_length": len(page_data.get("html_content", "")),
                            "images_found": len(page_data.get("images", []))})

            # Step 3: Assess confidence
            confidence = self._assess_confidence(page_data)
            result["confidence"] = confidence

            if confidence == "low":
                self.mlog.warning(url_key, page_type, "transform",
                                  "Low confidence in content fidelity - flagging for human review",
                                  {"reason": "Content may not match the live page layout"})
                result["status"] = STATUS_NEEDS_REVIEW
                # Still continue to upload as draft so human can review in Builder.io

            # Step 4: Process images
            if not dry_run:
                self.mlog.info(url_key, page_type, "images", "Processing images...")
                try:
                    updated_html, image_mappings = self.image_handler.process_images_in_html(
                        page_data["html_content"],
                        base_url=self.source_base_url,
                    )
                    page_data["html_content"] = updated_html
                    result["images_processed"] = len(image_mappings)
                    self.mlog.info(url_key, page_type, "images",
                                   f"Processed {len(image_mappings)} images")
                except Exception as e:
                    self.mlog.warning(url_key, page_type, "images",
                                     f"Image processing error (continuing): {e}")

                # Step 4a: Rewrite internal <a href> links that still point at
                # the old Magento platform. Live capture already does this in
                # the browser, but the legacy/fallback scrapers leave hrefs
                # untouched — without this, migrated pages keep linking back
                # to the source site.
                try:
                    page_data["html_content"] = self.image_handler.rewrite_internal_links(
                        page_data["html_content"],
                        source_base_url=self.source_base_url,
                    )
                except Exception as e:
                    self.mlog.warning(url_key, page_type, "transform",
                                     f"Link rewrite skipped: {e}")

                # Process thumbnail
                if page_data.get("thumbnail"):
                    try:
                        builder_thumbnail = self.image_handler.process_thumbnail(
                            page_data["thumbnail"]
                        )
                        if builder_thumbnail:
                            page_data["thumbnail"] = builder_thumbnail
                    except Exception as e:
                        self.mlog.warning(url_key, page_type, "images",
                                         f"Thumbnail processing error: {e}")

            # Step 4b: Deduplicate content blocks and images
            # Run explicitly here so page_data["html_content"] is clean for
            # both the upload step and the visual verification step.
            if not dry_run:
                html = page_data["html_content"]
                html, blocks_removed = deduplicate_content_blocks(html)
                html, imgs_removed = deduplicate_similar_images(html)
                page_data["html_content"] = html
                if blocks_removed or imgs_removed:
                    self.mlog.info(url_key, page_type, "transform",
                                   f"Deduplication: removed {blocks_removed} block(s), {imgs_removed} image(s)")

            # Step 4c: LLM-assisted layout verification BEFORE upload.
            # Render the processed HTML the same way Builder.io will, compare
            # it to a screenshot of the live source, and let an OpenAI vision
            # model rewrite the HTML if the layout has drifted. This is a
            # best-effort step — if OPENAI_API_KEY is unset or the call fails,
            # the pipeline falls through and uploads the unmodified HTML.
            # When live capture already pulled the real stylesheets, re-running
            # process_html_for_builder here would strip the <style> tags we
            # just collected and wipe out the visual fidelity. Skip the LLM
            # fixer entirely on live-captured HTML.
            already_final = bool(page_data.get("_html_already_processed"))
            if not dry_run and llm_fix_enabled() and not already_final:
                try:
                    self.mlog.info(url_key, page_type, "transform",
                                   "Running LLM-assisted layout fix against original page...")
                    # Feed the LLM the FULLY-processed HTML (the exact string
                    # that Builder.io will render), not the raw scraped HTML.
                    processed_for_builder = process_html_for_builder(
                        page_data["html_content"]
                    )
                    fixed_html = llm_fix_layout(
                        original_url=primary_url,
                        current_html=processed_for_builder,
                        url_key=url_key,
                    )
                    if fixed_html and fixed_html != processed_for_builder:
                        page_data["html_content"] = fixed_html
                        # Mark so the BuilderClient skips its own CSS pass
                        # (the fixer already returned Builder-ready HTML).
                        page_data["_html_already_processed"] = True
                        self.mlog.info(url_key, page_type, "transform",
                                       "LLM layout fix applied to HTML before upload")
                    else:
                        self.mlog.info(url_key, page_type, "transform",
                                       "LLM layout fix returned no changes")
                except Exception as e:
                    self.mlog.warning(url_key, page_type, "transform",
                                     f"LLM layout fix skipped (non-critical): {e}")

            # Step 5: Upload to Builder.io
            if dry_run:
                self.mlog.info(url_key, page_type, "upload",
                               f"[DRY RUN] Would create: {page_data.get('title', url_key)}")
                if result["status"] == STATUS_PENDING:
                    result["status"] = STATUS_PUBLISHED
                result["dry_run"] = True
            else:
                action = "Updating" if existing_entry_id else "Creating"
                self.mlog.info(url_key, page_type, "upload", f"{action} Builder.io entry...")

                # If low confidence, always save as draft for human review
                should_publish = publish and confidence != "low"

                api_result = self.builder.create_entry(
                    page_data, page_type=page_type, publish=should_publish,
                    existing_entry_id=existing_entry_id,
                )

                if api_result.get("success"):
                    result["builder_id"] = api_result.get("data", {}).get("id", "")

                    if result["status"] != STATUS_NEEDS_REVIEW:
                        result["status"] = STATUS_PUBLISHED

                    verb = "updated" if existing_entry_id else "created"
                    self.mlog.info(url_key, page_type, "upload",
                                   f"Successfully {verb}: {page_data.get('title', url_key)}",
                                   {"builder_id": result["builder_id"],
                                    "published": should_publish})
                else:
                    result["status"] = STATUS_FAILED
                    result["error"] = api_result.get("error", "Unknown error")
                    self.mlog.error(url_key, page_type, "upload",
                                    f"Upload failed: {result['error']}",
                                    {"details": api_result.get("details", "")})

            # Step 6: Visual verification (non-blocking — issues are logged but don't fail the migration)
            if not dry_run and result["status"] in (STATUS_PUBLISHED, STATUS_NEEDS_REVIEW):
                try:
                    self.mlog.info(url_key, page_type, "verify", "Running visual verification...")

                    # Build Builder.io preview URL for screenshot comparison
                    builder_preview_url = ""
                    if result.get("builder_id"):
                        model = self.builder.blog_model if page_type == "blog" else self.builder.page_model
                        builder_preview_url = self.builder.get_preview_url(
                            result["builder_id"], model_override=model
                        ) or ""

                    verification = run_visual_verification(
                        original_url=primary_url,
                        builder_preview_url=builder_preview_url,
                        page_html=page_data.get("html_content", ""),
                        url_key=url_key,
                    )
                    if verification.has_duplicates:
                        issues_str = "; ".join(verification.issues)
                        self.mlog.warning(url_key, page_type, "verify",
                                          f"Visual verification found issues: {issues_str}",
                                          {"duplicate_regions": len(verification.duplicate_regions)})
                        result["visual_issues"] = verification.issues
                        if result["status"] != STATUS_NEEDS_REVIEW:
                            result["status"] = STATUS_NEEDS_REVIEW
                            result["confidence"] = "medium"
                            self.results["needs_review"] += 1
                            self.results["success"] -= 1
                    else:
                        self.mlog.info(url_key, page_type, "verify",
                                       "Visual verification passed — no duplicates detected")
                except Exception as e:
                    self.mlog.warning(url_key, page_type, "verify",
                                     f"Visual verification skipped (non-critical): {e}")

        except Exception as e:
            result["status"] = STATUS_FAILED
            result["error"] = str(e)
            self.mlog.error(url_key, page_type, "error", f"Exception: {e}")

        return result

    def _scrape_page(self, url_key: str, page_type: str, primary_url: str) -> dict:
        """
        Fetch a page. When live capture is enabled we render the real URL in
        Playwright and keep the rendered DOM + the stylesheets the browser
        actually loaded. This is what makes the migrated page look identical
        to the source (carousels, sliders, hover states, fonts).

        If live capture fails (Playwright missing, network issue, bad selector),
        we fall back to the legacy GraphQL / HTML scrapers so the pipeline
        still produces a result.
        """
        # Live capture path — preferred, works for both page types.
        if self.use_live_capture and primary_url:
            capture = capture_live_fragment(
                primary_url,
                login=self.magento_login,
            )
            if capture.ok:
                # Reject soft-404 pages that Playwright rendered successfully
                # but whose title is an error-page title (e.g. "404 無法顯示頁面").
                # This happens when SOURCE_BLOG_PATH is wrong and every URL
                # resolves to the Pricerite 404 error page (HTTP 200).
                captured_title = capture.meta_title or capture.title or ""
                if _is_soft_404_title(captured_title):
                    self.mlog.error(
                        url_key, page_type, "scrape",
                        f"Live capture returned a soft-404 page (title: '{captured_title}'). "
                        "Check SOURCE_BLOG_PATH in .env — constructed URL is hitting the error page.",
                    )
                    return {
                        "error": (
                            f"Soft 404: live capture title is '{captured_title}'. "
                            "Verify SOURCE_BLOG_PATH includes the locale prefix (e.g. /hk/zh/news/)."
                        ),
                        "url_key": url_key,
                    }

                self.mlog.info(
                    url_key, page_type, "scrape",
                    f"Live capture OK: {capture.css_rule_count} CSS rules, "
                    f"selector={capture.content_selector_used!r}",
                )
                # Merge capture with whatever metadata the legacy scraper
                # already knew (like graphql tags/categories for blog posts).
                meta = {}
                if page_type == "blog":
                    legacy = self.blog_scraper.fetch_post_by_url_key(url_key) or {}
                    if legacy and not legacy.get("error"):
                        meta = legacy

                return {
                    "title": capture.title or meta.get("title", ""),
                    "html_content": capture.html_fragment,
                    "thumbnail": capture.og_image or meta.get("thumbnail", ""),
                    "og_image": capture.og_image,
                    "meta_title": capture.meta_title or meta.get("meta_title", ""),
                    "meta_description": capture.meta_description or meta.get("meta_description", ""),
                    "meta_keywords": meta.get("meta_keywords", ""),
                    "categories": meta.get("categories", []),
                    "tags": meta.get("tags", []),
                    "url_key": url_key,
                    "published_at": meta.get("published_at", ""),
                    "images": capture.images,
                    "source": "live_capture",
                    "page_type": page_type,
                    # Signal downstream that HTML is already final — skip
                    # css_processor + LLM rewriter, they'd strip the very
                    # stylesheets we just collected.
                    "_html_already_processed": True,
                }
            else:
                self.mlog.warning(
                    url_key, page_type, "scrape",
                    f"Live capture failed ({capture.error}); falling back to legacy scraper",
                )

        if page_type == "blog":
            return self.blog_scraper.fetch_post_by_url_key(url_key)
        else:
            return self.static_scraper.fetch_page_by_url(primary_url, url_key)

    def _assess_confidence(self, page_data: dict) -> str:
        """
        Assess confidence that the scraped content faithfully represents the live page.

        Returns: 'high', 'medium', or 'low'
        """
        html = page_data.get("html_content", "")
        title = page_data.get("title", "")
        source = page_data.get("source", "")

        # No content at all
        if not html or len(html) < 100:
            return "low"

        # No title found
        if not title:
            return "low"

        # Safety net: reject any content whose title looks like a 404 / error page.
        # The primary guard is in _scrape_page, but this catches edge cases where
        # partial content was returned alongside an error-page title.
        if _is_soft_404_title(title):
            return "low"

        # GraphQL/CMS API source is more reliable than HTML scraping
        if source in ("graphql", "cms_graphql"):
            return "high"

        # HTML scraping - check content quality
        content_length = len(html)

        # Very short content might be an error page
        if content_length < 500:
            return "low"

        # Moderate content
        if content_length < 2000:
            return "medium"

        return "high"

    def save_results(self, output_path: str = "migration_results.json"):
        """Save migration results to a JSON file."""
        Path("output").mkdir(exist_ok=True)
        path = Path("output") / output_path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        self.mlog.info("__pipeline__", "", "export", f"Results saved to {path}")

    def get_log_entries(self, page_key: str = None) -> list[dict]:
        """Get log entries, optionally filtered by page_key."""
        return self.mlog.get_entries(page_key=page_key)

    def get_log_summary(self) -> dict:
        """Get a summary of the migration log."""
        return self.mlog.get_summary()

    def export_log(self, output_path: str = None) -> str:
        """Export the full log."""
        return self.mlog.export_log(output_path)
