"""
Comprehensive logging module for the migration pipeline.

Every log entry includes a page_key (URL key or page identifier) so logs
can be filtered per-page after the run. Logs are written to:
1. Console (via standard logging)
2. A structured JSON log file (one JSON object per line)
3. An in-memory list for the Streamlit UI

Log format:
{
    "timestamp": "2026-03-28T10:30:00.123456",
    "page_key": "airconditioner_hp",
    "page_type": "blog",
    "step": "scrape",
    "level": "INFO",
    "message": "Successfully scraped blog post",
    "details": { ... optional extra data ... }
}
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class MigrationLogger:
    """Structured logger for the migration pipeline with page-key filtering."""

    STEPS = [
        "init",        # Pipeline initialization
        "excel_read",  # Reading Excel input
        "scrape",      # Scraping content from source
        "images",      # Processing/uploading images
        "transform",   # Transforming content for Builder.io
        "upload",      # Uploading to Builder.io
        "verify",      # Verifying upload
        "export",      # Exporting results
        "complete",    # Pipeline completion
        "error",       # Error handling
    ]

    def __init__(self, log_dir: str = "logs", run_id: str = None):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.log_dir / f"migration_{self.run_id}.jsonl"
        self._entries: list[dict] = []
        self._lock = Lock()

    def log(
        self,
        page_key: str,
        page_type: str,
        step: str,
        level: str,
        message: str,
        details: dict = None,
    ):
        """Write a structured log entry."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "run_id": self.run_id,
            "page_key": page_key,
            "page_type": page_type,
            "step": step,
            "level": level.upper(),
            "message": message,
        }
        if details:
            entry["details"] = details

        with self._lock:
            self._entries.append(entry)
            # Append to JSONL file
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"Failed to write log to file: {e}")

        # Also emit via standard logging
        log_msg = f"[{page_key}][{step}] {message}"
        log_level = getattr(logging, level.upper(), logging.INFO)
        logger.log(log_level, log_msg)

    def info(self, page_key: str, page_type: str, step: str, message: str, details: dict = None):
        self.log(page_key, page_type, step, "INFO", message, details)

    def warning(self, page_key: str, page_type: str, step: str, message: str, details: dict = None):
        self.log(page_key, page_type, step, "WARNING", message, details)

    def error(self, page_key: str, page_type: str, step: str, message: str, details: dict = None):
        self.log(page_key, page_type, step, "ERROR", message, details)

    def debug(self, page_key: str, page_type: str, step: str, message: str, details: dict = None):
        self.log(page_key, page_type, step, "DEBUG", message, details)

    def get_entries(self, page_key: str = None, step: str = None, level: str = None) -> list[dict]:
        """Get log entries, optionally filtered by page_key, step, or level."""
        with self._lock:
            entries = list(self._entries)

        if page_key:
            entries = [e for e in entries if e["page_key"] == page_key]
        if step:
            entries = [e for e in entries if e["step"] == step]
        if level:
            entries = [e for e in entries if e["level"] == level.upper()]

        return entries

    def get_page_keys(self) -> list[str]:
        """Get all unique page keys that have been logged."""
        with self._lock:
            return list(dict.fromkeys(e["page_key"] for e in self._entries))

    def get_summary(self) -> dict:
        """Get a summary of the migration run."""
        with self._lock:
            entries = list(self._entries)

        total_pages = len(set(e["page_key"] for e in entries))
        errors = [e for e in entries if e["level"] == "ERROR"]
        warnings = [e for e in entries if e["level"] == "WARNING"]

        return {
            "run_id": self.run_id,
            "log_file": str(self.log_file),
            "total_entries": len(entries),
            "total_pages": total_pages,
            "errors": len(errors),
            "warnings": len(warnings),
        }

    def export_log(self, output_path: str = None) -> str:
        """Export the full log to a JSON file."""
        if not output_path:
            output_path = str(self.log_dir / f"migration_{self.run_id}_full.json")

        with self._lock:
            entries = list(self._entries)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)

        return output_path
