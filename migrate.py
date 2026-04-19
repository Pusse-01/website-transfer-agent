#!/usr/bin/env python3
"""
Website Transfer Agent CLI - Migrate pages from Magento to Builder.io.

Supports both blog posts and static CMS pages.

Usage:
    # Migrate blog posts from Excel
    python migrate.py --excel Blog_Post_List.xlsx

    # Migrate static pages from Excel
    python migrate.py --excel Static_Page_List.xlsx

    # Migrate both in one run
    python migrate.py --blog-excel Blog_Post_List.xlsx --static-excel Static_Page_List.xlsx

    # Migrate specific blog posts by URL key
    python migrate.py --urls airconditioner_hp,dehumidifiers --type blog

    # Migrate specific static pages by URL key
    python migrate.py --urls member-point,customer-service-center --type static

    # Dry run (scrape only, don't upload)
    python migrate.py --excel Blog_Post_List.xlsx --dry-run

    # Publish immediately (default is draft)
    python migrate.py --excel Blog_Post_List.xlsx --publish

    # Limit number of pages
    python migrate.py --excel Blog_Post_List.xlsx --limit 3
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src.migration_agent import MigrationAgent
from src.excel_writer import export_blog_results

console = Console()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Migrate web pages from Magento to Builder.io",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Source options
    source_group = parser.add_argument_group("Input sources")
    source_group.add_argument(
        "--excel", "-e",
        help="Path to Excel file (auto-detects blog vs static page list)",
    )
    source_group.add_argument(
        "--blog-excel",
        help="Path to Blog Post List Excel file",
    )
    source_group.add_argument(
        "--static-excel",
        help="Path to Static Page List Excel file",
    )
    source_group.add_argument(
        "--urls", "-u",
        help="Comma-separated list of URL keys to migrate",
    )
    source_group.add_argument(
        "--type", choices=["blog", "static"], default="blog",
        help="Page type when using --urls (default: blog)",
    )

    # Builder.io options
    parser.add_argument(
        "--api-key",
        help="Builder.io Private API Key (or set BUILDER_API_KEY env var)",
    )

    # Source website options
    parser.add_argument(
        "--source-url",
        default=None,
        help="Source website base URL (default: from .env)",
    )
    parser.add_argument(
        "--blog-path",
        default="/news/",
        help="Blog path on source website (default: /news/)",
    )

    # Migration options
    parser.add_argument(
        "--publish", action="store_true",
        help="Publish posts immediately (default: save as draft)",
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true",
        help="Don't skip posts that already exist in Builder.io",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scrape content but don't upload to Builder.io",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Maximum number of pages to migrate (0 = all)",
    )
    parser.add_argument(
        "--priority", type=int, default=0,
        help="Only migrate posts with priority <= this value (0 = all)",
    )

    # General options
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--save-results", action="store_true",
        help="Save migration results to output/ directory",
    )

    args = parser.parse_args()

    if not any([args.excel, args.blog_excel, args.static_excel, args.urls]):
        parser.error("At least one input source is required: --excel, --blog-excel, --static-excel, or --urls")

    load_dotenv()
    setup_logging(args.verbose)

    # Resolve configuration
    api_key = args.api_key or os.getenv("BUILDER_API_KEY")
    if not api_key and not args.dry_run:
        console.print("[red]Error:[/red] Builder.io API key is required.")
        console.print("Set BUILDER_API_KEY in .env file or pass --api-key")
        sys.exit(1)

    source_url = args.source_url or os.getenv("SOURCE_BASE_URL", "https://www.pricerite.com.hk")
    public_key = os.getenv("BUILDER_PUBLIC_KEY", "")

    if args.dry_run and not api_key:
        api_key = "dry-run-placeholder"

    # Show configuration
    console.print("\n[bold]Website Transfer Agent[/bold]")
    console.print(f"  Source:     {source_url}")
    console.print(f"  Blog path:  {args.blog_path}")
    console.print(f"  Publish:    {'Yes' if args.publish else 'No (draft)'}")
    console.print(f"  Dry run:    {'Yes' if args.dry_run else 'No'}")
    console.print()

    # Initialize the migration agent
    agent = MigrationAgent(
        source_base_url=source_url,
        builder_api_key=api_key,
        builder_model="blog-post",
        blog_path=args.blog_path,
        builder_public_key=public_key,
    )

    # Run migration based on input
    if args.blog_excel or args.static_excel:
        results = agent.migrate_combined(
            blog_excel_path=args.blog_excel,
            static_excel_path=args.static_excel,
            publish=args.publish,
            skip_existing=not args.no_skip_existing,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.excel:
        results = agent.migrate_from_excel(
            excel_path=args.excel,
            publish=args.publish,
            skip_existing=not args.no_skip_existing,
            limit=args.limit,
            priority_filter=args.priority,
            dry_run=args.dry_run,
        )
    else:
        url_keys = [k.strip() for k in args.urls.split(",") if k.strip()]
        results = agent.migrate_from_url_keys(
            url_keys=url_keys,
            page_type=args.type,
            publish=args.publish,
            skip_existing=not args.no_skip_existing,
            dry_run=args.dry_run,
        )

    # Display results table
    table = Table(title="Migration Results")
    table.add_column("URL Key", style="cyan")
    table.add_column("Title", max_width=40)
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Confidence")
    table.add_column("Images")
    table.add_column("Error", style="red", max_width=30)

    for detail in results.get("details", []):
        status_style = {
            "published_by_agent": "[green]PUBLISHED[/green]",
            "failed": "[red]FAILED[/red]",
            "skipped": "[yellow]SKIPPED[/yellow]",
            "needs_human_review": "[magenta]REVIEW[/magenta]",
            "pending": "[dim]PENDING[/dim]",
        }.get(detail.get("status", ""), detail.get("status", ""))

        confidence_style = {
            "high": "[green]high[/green]",
            "medium": "[yellow]medium[/yellow]",
            "low": "[red]low[/red]",
        }.get(detail.get("confidence", ""), detail.get("confidence", ""))

        table.add_row(
            detail.get("url_key", ""),
            (detail.get("title", "") or "")[:40],
            detail.get("page_type", ""),
            status_style,
            confidence_style,
            str(detail.get("images_processed", "-")),
            detail.get("error", "") or "",
        )

    console.print(table)

    # Summary
    console.print(f"\n[bold]Total: {results['total']} | "
                  f"[green]Published: {results['success']}[/green] | "
                  f"[red]Failed: {results['failed']}[/red] | "
                  f"[yellow]Skipped: {results['skipped']}[/yellow] | "
                  f"[magenta]Needs Review: {results.get('needs_review', 0)}[/magenta][/bold]")

    if args.save_results:
        agent.save_results()
        excel_path = export_blog_results(results)
        if excel_path:
            console.print(f"\n[green]Results exported to:[/green] {excel_path}")
        log_path = agent.export_log()
        if log_path:
            console.print(f"[green]Full log exported to:[/green] {log_path}")

    if results["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
