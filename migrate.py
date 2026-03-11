#!/usr/bin/env python3
"""
Blog Migration CLI - Migrate blog posts from a website to Builder.io.

Usage:
    # Migrate all posts from Excel list
    python migrate.py --excel Blog_Post_List.xlsx

    # Migrate specific posts by URL key
    python migrate.py --urls airconditioner_hp,dehumidifiers,dehumidifier

    # Dry run (scrape only, don't upload)
    python migrate.py --excel Blog_Post_List.xlsx --dry-run

    # Migrate top priority posts only
    python migrate.py --excel Blog_Post_List.xlsx --priority 5

    # Publish immediately (default is draft)
    python migrate.py --excel Blog_Post_List.xlsx --publish

    # Limit number of posts
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
        description="Migrate blog posts from a website to Builder.io",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Source options
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--excel", "-e",
        help="Path to Excel file containing blog post list",
    )
    source_group.add_argument(
        "--urls", "-u",
        help="Comma-separated list of blog URL keys to migrate",
    )

    # Builder.io options
    parser.add_argument(
        "--api-key",
        help="Builder.io Private API Key (or set BUILDER_API_KEY env var)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Builder.io model name (default: blog-article)",
    )

    # Source website options
    parser.add_argument(
        "--source-url",
        default=None,
        help="Source website base URL (default: from .env)",
    )
    parser.add_argument(
        "--blog-path",
        default="/blog/",
        help="Blog path on source website (default: /blog/)",
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
        help="Maximum number of posts to migrate (0 = all)",
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
        help="Save migration results to output/migration_results.json",
    )

    args = parser.parse_args()

    # Load environment variables
    load_dotenv()

    setup_logging(args.verbose)

    # Resolve configuration
    api_key = args.api_key or os.getenv("BUILDER_API_KEY")
    if not api_key and not args.dry_run:
        console.print("[red]Error:[/red] Builder.io API key is required.")
        console.print("Set BUILDER_API_KEY in .env file or pass --api-key")
        sys.exit(1)

    source_url = args.source_url or os.getenv("SOURCE_BASE_URL", "https://www.pricerite.com.hk")
    model_name = args.model or os.getenv("BUILDER_MODEL_NAME", "blog-article")

    # For dry run, use a placeholder API key if none provided
    if args.dry_run and not api_key:
        api_key = "dry-run-placeholder"

    # Show configuration
    console.print("\n[bold]Blog Migration Agent[/bold]")
    console.print(f"  Source:     {source_url}")
    console.print(f"  Model:      {model_name}")
    console.print(f"  Blog path:  {args.blog_path}")
    console.print(f"  Publish:    {'Yes' if args.publish else 'No (draft)'}")
    console.print(f"  Dry run:    {'Yes' if args.dry_run else 'No'}")
    console.print()

    # Initialize the migration agent
    agent = MigrationAgent(
        source_base_url=source_url,
        builder_api_key=api_key,
        builder_model=model_name,
        blog_path=args.blog_path,
    )

    # Run migration
    if args.excel:
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
            publish=args.publish,
            skip_existing=not args.no_skip_existing,
            dry_run=args.dry_run,
        )

    # Display results table
    table = Table(title="Migration Results")
    table.add_column("URL Key", style="cyan")
    table.add_column("Title", max_width=40)
    table.add_column("Status")
    table.add_column("Images")
    table.add_column("Error", style="red", max_width=30)

    for detail in results.get("details", []):
        status_style = {
            "success": "[green]OK[/green]",
            "failed": "[red]FAIL[/red]",
            "skipped": "[yellow]SKIP[/yellow]",
        }.get(detail.get("status", ""), detail.get("status", ""))

        table.add_row(
            detail.get("url_key", ""),
            detail.get("title", "")[:40],
            status_style,
            str(detail.get("images_processed", "-")),
            detail.get("error", "") or "",
        )

    console.print(table)

    # Summary
    console.print(f"\n[bold]Total: {results['total']} | "
                  f"[green]Success: {results['success']}[/green] | "
                  f"[red]Failed: {results['failed']}[/red] | "
                  f"[yellow]Skipped: {results['skipped']}[/yellow][/bold]")

    if args.save_results:
        agent.save_results()

    # Exit with error code if any failures
    if results["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
