"""
Excel output writer for migration results.

Generates an Excel file with all original fields from the input Excel
plus extra columns for the migration status, Builder.io ID, confidence,
error messages, and other pipeline metadata.
"""

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def export_blog_results(results: dict, output_path: str = None) -> str:
    """
    Export blog post migration results to an Excel file.

    The output includes all original fields (Priority, ID, Categories, Title,
    Status, Url Key, Published At) plus:
    - Agent Status: published_by_agent / failed / skipped / needs_human_review / cancelled / pending
    - Builder ID: The Builder.io entry ID if created
    - Confidence: high / medium / low
    - Source: graphql / html / cms_graphql
    - Images Processed: count of images processed
    - Error: error message if any
    - Migrated At: timestamp of when this page was processed

    Args:
        results: Migration results dict from MigrationAgent
        output_path: Path for the output Excel file

    Returns:
        Path to the created Excel file
    """
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    except ImportError:
        logger.error("openpyxl is required: pip install openpyxl")
        return ""

    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        Path("output").mkdir(exist_ok=True)
        output_path = f"output/migration_results_{timestamp}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Migration Results"

    # Define styles
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    success_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    failed_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    review_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    skipped_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    # Headers
    headers = [
        "URL Key",
        "Title",
        "Page Type",
        "Category",
        "Original Status",
        "Priority",
        "Agent Status",
        "Confidence",
        "Builder ID",
        "Source Method",
        "Images Processed",
        "Meta Title",
        "Meta Description",
        "Primary URL",
        "Error",
        "Migrated At",
    ]

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    # Data rows
    details = results.get("details", [])
    for row_idx, detail in enumerate(details, 2):
        original = detail.get("original_data", {})

        values = [
            detail.get("url_key", ""),
            detail.get("title", "") or original.get("title", ""),
            detail.get("page_type", ""),
            original.get("categories", "") or original.get("category", ""),
            original.get("status", ""),
            original.get("priority", ""),
            detail.get("status", "pending"),
            detail.get("confidence", ""),
            detail.get("builder_id", ""),
            detail.get("source", ""),
            detail.get("images_processed", 0),
            detail.get("meta_title", ""),
            detail.get("meta_description", ""),
            original.get("primary_url", ""),
            detail.get("error", "") or "",
            results.get("completed_at", ""),
        ]

        for col, value in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=str(value) if value is not None else "")
            cell.border = thin_border

        # Color-code the Agent Status column (column 7)
        status_cell = ws.cell(row=row_idx, column=7)
        agent_status = detail.get("status", "")
        if agent_status == "published_by_agent":
            status_cell.fill = success_fill
        elif agent_status == "failed":
            status_cell.fill = failed_fill
        elif agent_status == "needs_human_review":
            status_cell.fill = review_fill
        elif agent_status == "skipped":
            status_cell.fill = skipped_fill

    # Auto-fit column widths (approximate)
    col_widths = [20, 40, 12, 15, 15, 10, 20, 12, 30, 15, 15, 35, 50, 50, 40, 22]
    for i, width in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

    # Add summary sheet
    ws_summary = wb.create_sheet("Summary")
    summary_data = [
        ["Migration Summary"],
        [""],
        ["Started At", results.get("started_at", "")],
        ["Completed At", results.get("completed_at", "")],
        [""],
        ["Total Pages", results.get("total", 0)],
        ["Published by Agent", results.get("success", 0)],
        ["Failed", results.get("failed", 0)],
        ["Skipped (existing)", results.get("skipped", 0)],
        ["Needs Human Review", results.get("needs_review", 0)],
    ]

    for row_idx, row_data in enumerate(summary_data, 1):
        for col_idx, value in enumerate(row_data, 1):
            cell = ws_summary.cell(row=row_idx, column=col_idx, value=value)
            if row_idx == 1:
                cell.font = Font(bold=True, size=14)
            elif col_idx == 1 and row_idx > 2:
                cell.font = Font(bold=True)

    ws_summary.column_dimensions["A"].width = 25
    ws_summary.column_dimensions["B"].width = 30

    # Freeze header row in main sheet
    ws.freeze_panes = "A2"

    # Save
    wb.save(output_path)
    logger.info(f"Migration results exported to {output_path}")
    return output_path
