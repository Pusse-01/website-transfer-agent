"""
Blog Migration Agent - Orchestrates the full blog migration pipeline.
Scrapes blog content from source website, processes images, and uploads to Builder.io.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .scraper import BlogScraper
from .image_handler import ImageHandler
from .builder_client import BuilderClient
from .excel_reader import read_blog_list

logger = logging.getLogger(__name__)


class MigrationAgent:
    """Orchestrates blog migration from source website to Builder.io."""

    def __init__(
        self,
        source_base_url: str,
        builder_api_key: str,
        builder_model: str = "blog-article",
        blog_path: str = "/blog/",
        download_dir: str = "downloaded_images",
    ):
        self.scraper = BlogScraper(source_base_url, blog_path)
        self.image_handler = ImageHandler(builder_api_key, download_dir)
        self.builder = BuilderClient(builder_api_key, builder_model)
        self.source_base_url = source_base_url

        # Migration state
        self.results = {
            "started_at": None,
            "completed_at": None,
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
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
    ) -> dict:
        """
        Migrate blog posts listed in an Excel file.

        Args:
            excel_path: Path to the Excel file with blog post list
            publish: Whether to publish posts immediately
            skip_existing: Skip posts that already exist in Builder.io
            limit: Max number of posts to migrate (0 = all)
            priority_filter: Only migrate posts with priority <= this value (0 = all)
            dry_run: If True, scrape content but don't upload to Builder.io
        """
        blog_posts = read_blog_list(excel_path)
        if not blog_posts:
            logger.error("No blog posts found in Excel file")
            return self.results

        # Apply priority filter
        if priority_filter > 0:
            blog_posts = [p for p in blog_posts if p.get("priority", 0) and p["priority"] <= priority_filter]

        # Apply limit
        if limit > 0:
            blog_posts = blog_posts[:limit]

        url_keys = [p["url_key"] for p in blog_posts]
        return self.migrate_posts(url_keys, publish, skip_existing, dry_run)

    def migrate_from_url_keys(
        self,
        url_keys: list[str],
        publish: bool = False,
        skip_existing: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Migrate specific blog posts by URL keys."""
        return self.migrate_posts(url_keys, publish, skip_existing, dry_run)

    def migrate_posts(
        self,
        url_keys: list[str],
        publish: bool = False,
        skip_existing: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Core migration logic - process a list of blog URL keys."""
        self.results["started_at"] = datetime.now().isoformat()
        self.results["total"] = len(url_keys)

        logger.info(f"Starting migration of {len(url_keys)} blog posts")
        if dry_run:
            logger.info("DRY RUN MODE - No content will be uploaded to Builder.io")

        for i, url_key in enumerate(url_keys, 1):
            logger.info(f"\n[{i}/{len(url_keys)}] Processing: {url_key}")
            result = self._migrate_single_post(url_key, publish, skip_existing, dry_run)
            self.results["details"].append(result)

            if result["status"] == "success":
                self.results["success"] += 1
            elif result["status"] == "skipped":
                self.results["skipped"] += 1
            else:
                self.results["failed"] += 1

            # Rate limit between posts
            if i < len(url_keys):
                time.sleep(1)

        self.results["completed_at"] = datetime.now().isoformat()
        self._print_summary()
        return self.results

    def _migrate_single_post(
        self, url_key: str, publish: bool, skip_existing: bool, dry_run: bool
    ) -> dict:
        """Migrate a single blog post."""
        result = {"url_key": url_key, "status": "pending", "error": None}

        try:
            # Check if already exists
            if skip_existing and not dry_run:
                if self.builder.check_entry_exists(url_key):
                    logger.info(f"  Skipping (already exists): {url_key}")
                    result["status"] = "skipped"
                    return result

            # Step 1: Scrape the blog post
            logger.info(f"  Scraping content...")
            post_data = self.scraper.fetch_post_by_url_key(url_key)

            if post_data.get("error"):
                result["status"] = "failed"
                result["error"] = post_data["error"]
                return result

            if not post_data.get("html_content"):
                result["status"] = "failed"
                result["error"] = "No content found"
                return result

            result["title"] = post_data.get("title", "")
            result["source"] = post_data.get("source", "")

            # Step 2: Process images (download + upload to Builder.io)
            if not dry_run:
                logger.info(f"  Processing images...")
                updated_html, image_mappings = self.image_handler.process_images_in_html(
                    post_data["html_content"],
                    base_url=self.source_base_url,
                )
                post_data["html_content"] = updated_html
                result["images_processed"] = len(image_mappings)

                # Process thumbnail
                if post_data.get("thumbnail"):
                    builder_thumbnail = self.image_handler.process_thumbnail(
                        post_data["thumbnail"]
                    )
                    if builder_thumbnail:
                        post_data["thumbnail"] = builder_thumbnail

            # Step 3: Create entry in Builder.io
            if dry_run:
                logger.info(f"  [DRY RUN] Would create: {post_data.get('title', url_key)}")
                result["status"] = "success"
                result["dry_run"] = True
            else:
                logger.info(f"  Creating Builder.io entry...")
                api_result = self.builder.create_blog_entry(post_data, publish=publish)

                if api_result.get("success"):
                    result["status"] = "success"
                    result["builder_id"] = api_result.get("data", {}).get("id", "")
                    logger.info(f"  Successfully migrated: {post_data.get('title', url_key)}")
                else:
                    result["status"] = "failed"
                    result["error"] = api_result.get("error", "Unknown error")
                    logger.error(f"  Failed: {result['error']}")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"  Exception: {e}")

        return result

    def _print_summary(self):
        """Print migration summary."""
        r = self.results
        logger.info("\n" + "=" * 60)
        logger.info("MIGRATION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total:    {r['total']}")
        logger.info(f"Success:  {r['success']}")
        logger.info(f"Failed:   {r['failed']}")
        logger.info(f"Skipped:  {r['skipped']}")
        logger.info(f"Started:  {r['started_at']}")
        logger.info(f"Finished: {r['completed_at']}")
        logger.info("=" * 60)

        if r["failed"] > 0:
            logger.info("\nFailed posts:")
            for d in r["details"]:
                if d["status"] == "failed":
                    logger.info(f"  - {d['url_key']}: {d.get('error', 'Unknown')}")

    def save_results(self, output_path: str = "migration_results.json"):
        """Save migration results to a JSON file."""
        Path("output").mkdir(exist_ok=True)
        path = Path("output") / output_path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        logger.info(f"Results saved to {path}")
