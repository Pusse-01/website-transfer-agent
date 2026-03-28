#!/usr/bin/env python3
"""
One-time script to import legacy migration files into the new
persistent runs structure (output/runs/<run_id>/).

Scans for:
- output/migration_results*.json files
- logs/migration_*.jsonl log files
- output/migration_results*.xlsx Excel files

And creates proper run entries so the dashboard can browse them.

Usage:
    python import_legacy_runs.py
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from src.state_persistence import save_run, list_runs


def find_legacy_files():
    """Find all legacy migration files."""
    results_jsons = sorted(Path("output").glob("migration_results*.json")) if Path("output").exists() else []
    # Exclude files inside runs/ directory
    results_jsons = [f for f in results_jsons if "runs" not in f.parts]

    log_jsonls = sorted(Path("logs").glob("migration_*.jsonl")) if Path("logs").exists() else []
    log_fulls = sorted(Path("logs").glob("migration_*_full.json")) if Path("logs").exists() else []
    excels = sorted(Path("output").glob("migration_results*.xlsx")) if Path("output").exists() else []
    # Exclude files inside runs/ directory
    excels = [f for f in excels if "runs" not in f.parts]

    return {
        "results_jsons": results_jsons,
        "log_jsonls": log_jsonls,
        "log_fulls": log_fulls,
        "excels": excels,
    }


def extract_run_id_from_filename(filename: str) -> str:
    """Extract a timestamp-based run ID from a filename like migration_20260328_155557.jsonl"""
    parts = filename.replace("migration_", "").replace("_full", "")
    parts = parts.replace("results_", "").replace(".jsonl", "").replace(".json", "").replace(".xlsx", "")
    return parts.strip("_") if parts else ""


def import_legacy():
    files = find_legacy_files()

    print("Found legacy files:")
    for key, paths in files.items():
        for p in paths:
            print(f"  [{key}] {p}")

    if not any(files.values()):
        print("\nNo legacy files found. Nothing to import.")
        return

    existing_runs = {r["run_id"] for r in list_runs()}
    imported = 0

    # Strategy: match files by timestamp in filename
    # Group JSONL logs by their run ID
    log_map = {}  # run_id -> jsonl path
    for jsonl in files["log_jsonls"]:
        rid = extract_run_id_from_filename(jsonl.name)
        if rid:
            log_map[rid] = jsonl

    full_log_map = {}
    for full in files["log_fulls"]:
        rid = extract_run_id_from_filename(full.name)
        if rid:
            full_log_map[rid] = full

    excel_map = {}
    for xlsx in files["excels"]:
        rid = extract_run_id_from_filename(xlsx.name)
        if rid:
            excel_map[rid] = xlsx

    # Process results JSON files
    for results_json in files["results_jsons"]:
        rid = extract_run_id_from_filename(results_json.name)

        # If it's the generic "migration_results.json" without a timestamp, use file mtime
        if not rid or rid == "migration_results" or rid == "migration":
            mtime = datetime.fromtimestamp(results_json.stat().st_mtime)
            rid = f"legacy_{mtime.strftime('%Y%m%d_%H%M%S')}"

        if rid in existing_runs:
            print(f"  Skipping {rid} (already exists)")
            continue

        try:
            with open(results_json, "r", encoding="utf-8") as f:
                migration_results = json.load(f)
        except Exception as e:
            print(f"  Failed to read {results_json}: {e}")
            continue

        # Find matching log entries
        pipeline_log = []
        log_file_path = ""

        # Try full JSON log first
        for log_rid, log_path in full_log_map.items():
            if log_rid.startswith(rid[:15]):  # Match by date prefix
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        pipeline_log = json.load(f)
                    log_file_path = str(log_path)
                except Exception:
                    pass
                break

        # Try JSONL log
        if not pipeline_log:
            for log_rid, log_path in log_map.items():
                if log_rid.startswith(rid[:15]):
                    try:
                        entries = []
                        with open(log_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    entries.append(json.loads(line))
                        pipeline_log = entries
                        log_file_path = str(log_path)
                    except Exception:
                        pass
                    break

        # Find matching Excel
        excel_path = ""
        for xlsx_rid, xlsx_path in excel_map.items():
            if xlsx_rid.startswith(rid[:15]):
                excel_path = str(xlsx_path)
                break

        save_run(
            run_id=rid,
            migration_results=migration_results,
            pipeline_log=pipeline_log,
            results_excel_path=excel_path,
            log_file_path=log_file_path,
        )
        imported += 1
        print(f"  Imported: {rid} ({migration_results.get('total', '?')} pages)")

    # Also import any JSONL logs that don't have a matching results JSON
    # (runs where the agent crashed before saving results)
    for log_rid, jsonl_path in log_map.items():
        if log_rid in existing_runs or any(log_rid.startswith(r[:15]) for r in existing_runs):
            continue

        try:
            entries = []
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))

            if not entries:
                continue

            # Reconstruct a minimal results dict from log entries
            page_keys = set(e.get("page_key", "") for e in entries if e.get("page_key") != "__pipeline__")
            errors = [e for e in entries if e.get("level") == "ERROR"]

            migration_results = {
                "started_at": entries[0].get("timestamp", ""),
                "completed_at": entries[-1].get("timestamp", ""),
                "total": len(page_keys),
                "success": 0,
                "failed": len(set(e.get("page_key") for e in errors)),
                "skipped": 0,
                "needs_review": 0,
                "details": [],
                "_imported_from_log_only": True,
            }

            save_run(
                run_id=log_rid,
                migration_results=migration_results,
                pipeline_log=entries,
                log_file_path=str(jsonl_path),
            )
            imported += 1
            print(f"  Imported (log only): {log_rid} ({len(page_keys)} pages in logs)")

        except Exception as e:
            print(f"  Failed to import {jsonl_path}: {e}")

    print(f"\nDone! Imported {imported} run(s).")
    print("You can now browse them in the dashboard under Results > Past Runs.")


if __name__ == "__main__":
    import_legacy()
