"""Task execution logger — writes durable records to sali-works/tasks/.

Each task gets a directory under ~/Desktop/sali-works/tasks/<task_id>/ containing:
  meta.json       — task objective, workspace, steps, timestamps
  progress.json   — step statuses, checkpoints, retry counts
  executions.json — tool execution history
  artifacts.json  — recorded file artifacts
  events.jsonl    — append-only event log (one JSON object per line)

These files are a human-readable, filesystem-level backup of the database state.
They survive even if the database is lost, and can be inspected without SQL.
The database remains authoritative during execution; sali-works is the archive.

When a task completes, its full record is saved to sali-works/tasks/ and then
the DB rows are cleaned up — so Sali learns from the experience (via the learning
pipeline which reads tool_execution) but doesn't accumulate stale task rows.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.task_logger")

_TASKS_DIR = "tasks"


def _tasks_base() -> Path:
    """The root tasks directory: ~/Desktop/sali-works/tasks/."""
    return Path.home() / "Desktop" / "sali-works" / _TASKS_DIR


def _task_dir(task_id: UUID, workspace: str | None = None) -> Path:
    """Resolve the sali-works directory for a task.

    Uses ~/Desktop/sali-works/tasks/<task_id>/. Creates the directory if it doesn't exist.
    """
    task_dir = _tasks_base() / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def _write_json(path: Path, data: Any) -> None:
    """Write JSON data atomically (write-then-rename pattern)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.rename(path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def save_task_meta(task_id: UUID, objective: str, steps: list[str],
                   *, workspace_root: str | None = None,
                   status: str = "open") -> Path:
    """Write task metadata to sali-works/tasks/<task_id>/meta.json."""
    task_dir = _task_dir(task_id, workspace_root)
    meta = {
        "task_id": str(task_id),
        "objective": objective,
        "steps": steps,
        "workspace_root": workspace_root,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_json(task_dir / "meta.json", meta)
    log.info("task_meta_saved", task_id=str(task_id), dir=str(task_dir))
    return task_dir


def append_event(task_id: UUID, event_type: str, payload: dict[str, Any] | None = None,
                 *, workspace_root: str | None = None) -> None:
    """Append a task event to sali-works/tasks/<task_id>/events.jsonl."""
    task_dir = _task_dir(task_id, workspace_root)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event_type,
        **(payload or {}),
    }
    _append_jsonl(task_dir / "events.jsonl", record)


def save_checkpoint(task_id: UUID, step_seq: int, checkpoint: dict[str, Any],
                    *, workspace_root: str | None = None) -> None:
    """Save a step checkpoint to sali-works/tasks/<task_id>/progress.json."""
    task_dir = _task_dir(task_id, workspace_root)
    progress_path = task_dir / "progress.json"
    # Load existing progress or start fresh
    progress: dict[str, Any] = {}
    if progress_path.exists():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            progress = {}
    checkpoints = progress.get("checkpoints", {})
    checkpoints[str(step_seq)] = {
        "data": checkpoint,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    progress["checkpoints"] = checkpoints
    progress["last_updated"] = datetime.now(UTC).isoformat()
    _write_json(progress_path, progress)


def save_task_record(task_id: UUID, task_row: dict[str, Any],
                     steps: list[dict[str, Any]],
                     executions: list[dict[str, Any]] | None = None,
                     artifacts: list[dict[str, Any]] | None = None,
                     reviews: list[dict[str, Any]] | None = None) -> Path:
    """Write a complete task snapshot to sali-works/tasks/<task_id>/.

    Called after task creation, step completion, and task completion.
    The database remains authoritative during execution — this is the archive.
    """
    task_dir = _task_dir(task_id, task_row.get("workspace_root"))

    # Meta
    meta = {
        "task_id": str(task_id),
        "objective": task_row.get("objective", ""),
        "status": task_row.get("status", "unknown"),
        "workspace_root": task_row.get("workspace_root"),
        "workspace_mode": task_row.get("workspace_mode", "none"),
        "is_primary": task_row.get("is_primary", False),
        "max_retries": task_row.get("max_retries", 3),
        "retry_count": task_row.get("retry_count", 0),
        "created_at": str(task_row.get("created_at", "")),
        "updated_at": str(task_row.get("updated_at", "")),
    }
    if task_row.get("result"):
        meta["result"] = task_row["result"]
    _write_json(task_dir / "meta.json", meta)

    # Progress (steps + checkpoints)
    progress = {
        "steps": [
            {
                "seq": s.get("seq"),
                "description": s.get("description", ""),
                "status": s.get("status", "pending"),
                "note": s.get("note"),
                "attempts": s.get("attempts", 0),
                "last_error": s.get("last_error"),
                "failure_class": s.get("failure_class"),
                "verified": s.get("verified", False),
                "checkpoint": s.get("checkpoint"),
            }
            for s in steps
        ],
        "last_updated": datetime.now(UTC).isoformat(),
    }
    _write_json(task_dir / "progress.json", progress)

    # Executions
    if executions is not None:
        _write_json(task_dir / "executions.json", executions)

    # Artifacts
    if artifacts is not None:
        _write_json(task_dir / "artifacts.json", artifacts)

    # Reviews — the reviewer-gate history (Prompt 4). CASCADE removes task_review from the DB when the
    # task is deleted on completion; this keeps the durable audit trail (what was checked, what failed).
    if reviews is not None:
        _write_json(task_dir / "reviews.json", reviews)

    log.info("task_record_saved", task_id=str(task_id), dir=str(task_dir))
    return task_dir


def delete_task_folder(task_id: UUID) -> bool:
    """Delete the task's sali-works/tasks/<id>/ folder after user confirms satisfaction.

    Called only when the user explicitly confirms the task result is acceptable.
    The learning pipeline has already extracted knowledge from tool_execution rows
    (which persist in the DB), so deleting the folder doesn't lose learning data.
    """
    task_dir = _tasks_base() / str(task_id)
    if not task_dir.exists():
        return False
    import shutil
    shutil.rmtree(task_dir)
    log.info("task_folder_deleted", task_id=str(task_id), dir=str(task_dir))
    return True


def list_task_folders() -> list[dict[str, Any]]:
    """List all task folders in sali-works/tasks/ with their meta.json contents."""
    base = _tasks_base()
    if not base.exists():
        return []
    result = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        meta = {}
        if meta_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result.append({"task_id": d.name, "dir": str(d), "meta": meta})
    return result


def archive_completed_task(pool: Any, task_id: UUID) -> bool:
    """Archive a completed task to sali-works/tasks/ and clean up DB rows.

    1. Fetch full task state (steps, executions, artifacts)
    2. Write final snapshot to sali-works/tasks/<task_id>/
    3. Delete DB rows (task cascades to task_step, task_execution, task_artifact)
    4. Log the archival event

    The learning pipeline has already extracted what it needs from tool_execution
    before this runs, so deleting the task rows doesn't lose learning data.

    Returns True if the task was archived and cleaned up.
    """
    import asyncpg

    async def _archive() -> bool:
        conn = await asyncpg.connect(
            host="localhost", user="almir", database="sali",
            server_settings={"search_path": "sali, public"})
        try:
            task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            if task is None:
                return False
            if task["status"] not in ("done", "failed", "abandoned", "cancelled"):
                return False  # only archive terminal tasks

            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
            executions = await conn.fetch(
                "SELECT id, step_seq, tool_name, status, result_summary, error, "
                "  attempt, idempotent, started_at, finished_at "
                "FROM task_execution WHERE task_id = $1 ORDER BY started_at", task_id)
            artifacts = await conn.fetch(
                "SELECT artifact_path, artifact_type, tool_name, created_at "
                "FROM task_artifact WHERE task_id = $1 ORDER BY created_at", task_id)

            # Save final snapshot to sali-works
            save_task_record(
                task_id,
                dict(task),
                [dict(s) for s in steps],
                executions=[dict(e) for e in executions],
                artifacts=[dict(a) for a in artifacts])
            append_event(task_id, "task_archived",
                         {"status": task["status"], "objective": task["objective"]})

            # Clean up DB rows (CASCADE handles task_step, task_execution, task_artifact)
            await conn.execute("DELETE FROM task WHERE id = $1", task_id)
            log.info("task_archived_and_cleaned", task_id=str(task_id),
                     objective=task["objective"])
            return True
        finally:
            await conn.close()

    import asyncio
    try:
        return asyncio.run(_archive())
    except Exception as exc:
        log.warning("task_archive_failed", task_id=str(task_id), error=str(exc))
        return False
