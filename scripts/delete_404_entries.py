#!/usr/bin/env python3
"""
Remediation script: delete all Builder.io entries whose name contains a soft-404
title pattern (e.g. "404 無法顯示頁面").

These entries were created when SOURCE_BLOG_PATH was set to /news/ instead of
/hk/zh/news/, causing the scraper to hit Pricerite's 404 error page (which
returns HTTP 200) and upload its content to Builder.io.

Usage:
    python scripts/delete_404_entries.py [--model blog-post] [--dry-run]

Options:
    --model MODEL   Builder.io model to clean up (default: blog-post)
    --dry-run       List matching entries without deleting them
    --limit N       Stop after deleting N entries (default: 0 = all)
"""

import argparse
import os
import sys
import time

# Allow running from the project root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()


# Patterns that identify a soft-404 entry name.
SOFT_404_MARKERS = (
    "404",
    "無法顯示",
    "找不到",
    "not found",
    "page not found",
)


def is_404_entry(name: str) -> bool:
    name_lower = name.lower()
    for marker in SOFT_404_MARKERS:
        if marker in name or marker in name_lower:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Delete soft-404 entries from Builder.io")
    parser.add_argument("--model", default="blog-post", help="Builder.io model name")
    parser.add_argument("--dry-run", action="store_true", help="List entries without deleting")
    parser.add_argument("--limit", type=int, default=0, help="Max entries to delete (0=all)")
    args = parser.parse_args()

    api_key = os.getenv("BUILDER_API_KEY", "")
    public_key = os.getenv("BUILDER_PUBLIC_KEY", "")
    if not api_key:
        print("ERROR: BUILDER_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    from src.builder_client import BuilderClient

    client = BuilderClient(api_key, model_name=args.model, public_key=public_key)

    print(f"Fetching entries from model '{args.model}'…")
    # Fetch in pages of 100 until we have them all.
    all_entries = []
    page_size = 100
    offset = 0
    while True:
        batch = client.list_entries(limit=page_size, offset=offset, model_override=args.model)
        if not batch:
            break
        all_entries.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.3)

    print(f"Total entries fetched: {len(all_entries)}")

    targets = [e for e in all_entries if is_404_entry(e.get("name", ""))]
    print(f"Entries matching soft-404 pattern: {len(targets)}")

    if not targets:
        print("Nothing to delete.")
        return

    if args.dry_run:
        print("\n[DRY RUN] Would delete:")
        for e in targets:
            print(f"  id={e['id']}  name={e.get('name', '')!r}  slug={e.get('data', {}).get('slug', '')!r}")
        return

    deleted = 0
    failed = 0
    limit = args.limit or len(targets)

    for i, entry in enumerate(targets[:limit], 1):
        entry_id = entry.get("id", "")
        name = entry.get("name", "")
        slug = entry.get("data", {}).get("slug", "")
        result = client.delete_entry(entry_id, model_override=args.model)
        if result.get("success"):
            deleted += 1
            print(f"[{i}/{min(limit, len(targets))}] Deleted: {name!r}  ({slug})")
        else:
            failed += 1
            print(f"[{i}/{min(limit, len(targets))}] FAILED:  {name!r}  error={result.get('error')}")

    print(f"\nDone — deleted {deleted}, failed {failed}.")


if __name__ == "__main__":
    main()
