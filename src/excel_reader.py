"""
Excel reader module for loading blog post lists from spreadsheets.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_blog_list(excel_path: str) -> list[dict]:
    """
    Read blog post list from an Excel file.
    Expected columns: Priority, ID, Categories, Title, Status, Url Key

    Returns:
        List of dicts with keys: priority, id, categories, title, status, url_key
    """
    try:
        import openpyxl
    except ImportError:
        logger.error("openpyxl is required: pip install openpyxl")
        return []

    path = Path(excel_path)
    if not path.exists():
        logger.error(f"Excel file not found: {excel_path}")
        return []

    try:
        wb = openpyxl.load_workbook(path, read_only=True)

        # Try to find the blog post list sheet
        sheet = None
        for name in wb.sheetnames:
            if "blog" in name.lower() or "post" in name.lower():
                sheet = wb[name]
                break

        if sheet is None:
            # Use second sheet if available (first is often "Introduction")
            if len(wb.sheetnames) > 1:
                sheet = wb[wb.sheetnames[1]]
            else:
                sheet = wb.active

        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []

        # Find header row
        header_row = rows[0]
        headers = [str(h).strip().lower().replace(" ", "_") if h else "" for h in header_row]

        # Map column indices
        col_map = {}
        for i, h in enumerate(headers):
            if "priority" in h:
                col_map["priority"] = i
            elif h == "id":
                col_map["id"] = i
            elif "categor" in h:
                col_map["categories"] = i
            elif "title" in h:
                col_map["title"] = i
            elif "status" in h:
                col_map["status"] = i
            elif "url" in h and "key" in h:
                col_map["url_key"] = i
            elif "url_key" in h:
                col_map["url_key"] = i

        if "url_key" not in col_map:
            logger.error(f"Could not find 'Url Key' column in sheet. Headers: {headers}")
            return []

        blog_posts = []
        for row in rows[1:]:
            if not row or all(cell is None for cell in row):
                continue

            url_key = row[col_map["url_key"]] if col_map.get("url_key") is not None else None
            if not url_key:
                continue

            post = {
                "priority": row[col_map["priority"]] if col_map.get("priority") is not None else 0,
                "id": row[col_map["id"]] if col_map.get("id") is not None else "",
                "categories": row[col_map["categories"]] if col_map.get("categories") is not None else "",
                "title": row[col_map["title"]] if col_map.get("title") is not None else "",
                "status": row[col_map["status"]] if col_map.get("status") is not None else "",
                "url_key": str(url_key).strip(),
            }
            blog_posts.append(post)

        wb.close()
        logger.info(f"Loaded {len(blog_posts)} blog posts from {excel_path}")
        return blog_posts

    except Exception as e:
        logger.error(f"Failed to read Excel file: {e}")
        return []
