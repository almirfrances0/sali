"""WorkspaceCleanupStore (§24/§25/§26/§27) — the durable, resumable workspace-cleanup lifecycle.

When a task reaches a verified completion, an EPHEMERAL task workspace (mode 'auto', under
<sali-works>/tasks/<task_id>/) should be cleaned up to reclaim space — but only AFTER experience has
been extracted (the caller guarantees the ordering), and NEVER a user-owned workspace (mode explicit /
inherited, §25). Cleanup is recorded durably so a failure is visible and resumable, and is never
silently pretended-done (§26). Deleting the physical files does not delete Sali's memory of the task —
the experience already lives in durable memory (§27).

Safety: physical deletion is triple-guarded — it happens ONLY for an 'auto' workspace whose path is the
task-specific `.../sali-works/tasks/<task_id>` directory. A shared root, a user directory, or any path
that does not match the task id is never touched.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.tasks.cleanup")

_MODE_TO_TYPE = {"auto": "ephemeral", "explicit": "user_owned", "inherited": "user_owned",
                 "none": "none"}


def _is_ephemeral_task_dir(root: str | None, task_id: UUID) -> bool:
    """True only for the task's OWN ephemeral directory: <…>/sali-works/tasks/<task_id>. This is the
    single gate that authorises physical deletion — never a shared root or a user path (§25).

    Resolves the REAL path (realpath) before matching so a symlinked or traversal-laden `root` cannot
    smuggle deletion outside the ephemeral tasks tree, and matches the `tasks` parent as a real path
    component whose grandparent dir is literally named `sali-works` (not a substring anywhere in the
    string). Final audit §22/§23 — cleanup authority is checked adversarially, not by string shape."""
    if not root:
        return False
    try:
        p = Path(root).resolve()   # realpath: a symlinked/traversal-laden root cannot escape the check
    except (OSError, RuntimeError):
        return False
    return (p.name == str(task_id) and p.parent.name == "tasks" and "sali-works" in str(p))


class WorkspaceCleanupStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def cleanup_task(self, task_id: UUID) -> dict[str, Any]:
        """Determine the workspace type from the (still-present) task and act: delete an ephemeral task
        dir, skip a user-owned one, record the outcome durably. Idempotent + resumable. Returns the
        cleanup record. Reads the task BEFORE the caller archives/deletes it."""
        async with self._pool.acquire() as conn:
            task = await conn.fetchrow(
                "SELECT workspace_root, workspace_mode FROM task WHERE id=$1", task_id)
        if task is None or not task["workspace_root"]:
            return {"status": "skipped", "reason": "no workspace"}
        root = task["workspace_root"]
        ws_type = _MODE_TO_TYPE.get(task["workspace_mode"] or "none", "user_owned")
        policy = "auto" if ws_type == "ephemeral" else "keep"

        cid = await self._record(task_id, root, ws_type, policy)
        if policy == "keep":
            await self._finish(cid, "skipped", reason=f"{ws_type} workspace is never auto-deleted")
            return {"status": "skipped", "workspace_type": ws_type, "root": root}
        return await self._delete(cid, task_id, root)

    async def _delete(self, cleanup_id: UUID, task_id: UUID, root: str) -> dict[str, Any]:
        if not _is_ephemeral_task_dir(root, task_id):
            await self._finish(cleanup_id, "skipped", reason="path is not the task's ephemeral dir")
            return {"status": "skipped", "reason": "unsafe path — refused", "root": root}
        await self._mark(cleanup_id, "started")
        await self._emit("workspace.cleanup_started", task_id, {"root": root})
        try:
            p = Path(root)
            if p.exists():
                shutil.rmtree(p)
            await self._finish(cleanup_id, "completed")
            await self._emit("workspace.cleanup_completed", task_id, {"root": root})
            return {"status": "completed", "root": root}
        except Exception as exc:  # noqa: BLE001 - cleanup failure is DURABLE + resumable, never fatal (§26)
            await self._finish(cleanup_id, "failed", reason=str(exc)[:200])
            await self._emit("workspace.cleanup_failed", task_id, {"root": root, "error": str(exc)[:200]})
            log.warning("workspace_cleanup_failed", task_id=str(task_id), root=root, error=str(exc))
            return {"status": "failed", "root": root, "error": str(exc)[:200]}

    async def resume_pending(self, *, limit: int = 20) -> int:
        """Retry cleanups left pending/failed by a crash or an earlier error (§26/§52). Returns how many
        completed this pass. A directory that is already gone counts as completed (idempotent)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, workspace_root FROM workspace_cleanup "
                "WHERE status IN ('pending','failed') AND policy='auto' "
                "ORDER BY requested_at LIMIT $1", limit)
        done = 0
        for r in rows:
            tid = r["task_id"]
            if tid is None or not _is_ephemeral_task_dir(r["workspace_root"], tid):
                await self._finish(r["id"], "skipped", reason="not resumable safely")
                continue
            await self._mark(r["id"], "started")
            try:
                p = Path(r["workspace_root"])
                if p.exists():
                    shutil.rmtree(p)
                await self._finish(r["id"], "completed")
                await self._emit("workspace.cleanup_completed", tid, {"root": r["workspace_root"]})
                done += 1
            except Exception as exc:  # noqa: BLE001 - stays failed, still resumable next time
                await self._finish(r["id"], "failed", reason=str(exc)[:200])
        return done

    async def pending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, workspace_root, workspace_type, status, reason, requested_at "
                "FROM workspace_cleanup WHERE status IN ('pending','failed') "
                "ORDER BY requested_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def for_task(self, task_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, workspace_root, workspace_type, policy, status, reason, completed_at "
                "FROM workspace_cleanup WHERE task_id=$1 ORDER BY requested_at DESC LIMIT 1", task_id)
        return dict(row) if row else None

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM workspace_cleanup GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"pending": by.get("pending", 0) + by.get("failed", 0), "by_status": by}

    async def _record(self, task_id: UUID, root: str, ws_type: str, policy: str) -> UUID:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO workspace_cleanup (task_id, workspace_root, workspace_type, policy) "
                "VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (workspace_root, coalesce(task_id::text,'')) DO UPDATE SET status='pending' "
                "RETURNING id", task_id, root, ws_type, policy)
        return UUID(str(row["id"]))

    async def _mark(self, cleanup_id: UUID, status: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE workspace_cleanup SET status=$2 WHERE id=$1", cleanup_id, status)

    async def _finish(self, cleanup_id: UUID, status: str, *, reason: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE workspace_cleanup SET status=$2, reason=coalesce($3, reason), "
                "  completed_at=now() WHERE id=$1", cleanup_id, status, reason)

    async def _emit(self, event_type: str, task_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id,
                                       subject_type="workspace", origin="runtime", data=data)
