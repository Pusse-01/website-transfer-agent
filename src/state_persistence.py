"""
Persistent state management for the migration dashboard.

Saves and loads migration state to/from disk so that results, logs,
and pipeline status survive page refreshes and server restarts.

Files are stored under output/ and logs/ directories:
- output/runs/<run_id>/results.json   — migration results
- output/runs/<run_id>/state.json     — full UI state snapshot
- logs/migration_<run_id>.jsonl       — structured log (already created by MigrationLogger)
- logs/migration_<run_id>_full.json   — full log export

A manifest file (output/runs/manifest.json) indexes all runs for quick lookup.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

RUNS_DIR = Path("output/runs")
LOGS_DIR = Path("logs")


def _ensure_dirs():
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def save_run(
    run_id: str,
    migration_results: dict,
    pipeline_log: list[dict],
    results_excel_path: str = "",
    log_file_path: str = "",
):
    """
    Save a complete migration run to disk.

    Args:
        run_id: Unique identifier for this run (typically timestamp-based)
        migration_results: The agent.results dict
        pipeline_log: List of log entry dicts
        results_excel_path: Path to the generated Excel report
        log_file_path: Path to the full log JSON export
    """
    _ensure_dirs()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save results
    results_path = run_dir / "results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(migration_results, f, indent=2, ensure_ascii=False)

    # Save log entries
    log_path = run_dir / "log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(pipeline_log, f, indent=2, ensure_ascii=False)

    # Save state metadata
    state = {
        "run_id": run_id,
        "saved_at": datetime.now().isoformat(),
        "results_file": str(results_path),
        "log_file": str(log_path),
        "results_excel_path": results_excel_path,
        "log_jsonl_path": log_file_path,
        "total": migration_results.get("total", 0),
        "success": migration_results.get("success", 0),
        "failed": migration_results.get("failed", 0),
        "skipped": migration_results.get("skipped", 0),
        "needs_review": migration_results.get("needs_review", 0),
        "started_at": migration_results.get("started_at", ""),
        "completed_at": migration_results.get("completed_at", ""),
    }
    state_path = run_dir / "state.json"
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    # Update manifest
    _update_manifest(run_id, state)

    logger.info(f"Saved run {run_id} to {run_dir}")
    return str(run_dir)


def _update_manifest(run_id: str, state: dict):
    """Add or update a run entry in the manifest file."""
    manifest_path = RUNS_DIR / "manifest.json"
    manifest = []

    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = []

    # Remove existing entry for this run_id if any
    manifest = [m for m in manifest if m.get("run_id") != run_id]

    # Add new entry at the top
    manifest.insert(0, {
        "run_id": run_id,
        "saved_at": state.get("saved_at", ""),
        "started_at": state.get("started_at", ""),
        "completed_at": state.get("completed_at", ""),
        "total": state.get("total", 0),
        "success": state.get("success", 0),
        "failed": state.get("failed", 0),
        "skipped": state.get("skipped", 0),
        "needs_review": state.get("needs_review", 0),
        "results_excel_path": state.get("results_excel_path", ""),
    })

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def list_runs() -> list[dict]:
    """
    List all saved migration runs, most recent first.

    Returns:
        List of run metadata dicts from the manifest.
    """
    manifest_path = RUNS_DIR / "manifest.json"
    if not manifest_path.exists():
        return []

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read manifest: {e}")
        return []


def load_run(run_id: str) -> dict | None:
    """
    Load a complete migration run from disk.

    Returns:
        Dict with keys: run_id, migration_results, pipeline_log,
        results_excel_path, log_file_path, state
        Or None if not found.
    """
    run_dir = RUNS_DIR / run_id

    if not run_dir.exists():
        logger.warning(f"Run directory not found: {run_dir}")
        return None

    result = {"run_id": run_id}

    # Load state metadata
    state_path = run_dir / "state.json"
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            result["state"] = json.load(f)
    else:
        result["state"] = {}

    # Load results
    results_path = run_dir / "results.json"
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8") as f:
            result["migration_results"] = json.load(f)
    else:
        result["migration_results"] = None

    # Load log entries
    log_path = run_dir / "log.json"
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            result["pipeline_log"] = json.load(f)
    else:
        result["pipeline_log"] = []

    # Resolve file paths
    result["results_excel_path"] = result["state"].get("results_excel_path", "")
    result["log_file_path"] = result["state"].get("log_jsonl_path", "")

    return result


def load_latest_run() -> dict | None:
    """Load the most recent migration run."""
    runs = list_runs()
    if not runs:
        return None
    return load_run(runs[0]["run_id"])


def delete_run(run_id: str) -> bool:
    """Delete a saved migration run."""
    import shutil

    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        return False

    try:
        shutil.rmtree(run_dir)

        # Update manifest
        manifest_path = RUNS_DIR / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            manifest = [m for m in manifest if m.get("run_id") != run_id]
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)

        logger.info(f"Deleted run {run_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete run {run_id}: {e}")
        return False
