"""TaskStore — persist and advance multi-step tasks (spec §24).

A task and its steps live in the datastore, so they survive process restarts: the loop reads the
open tasks back into context each turn and Sali resumes. The store keeps the task's own status in
sync with its steps (all steps done → the task is done), so "is this finished?" is never guessed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.errors import classify_failure
from sali.tasks.models import Task, row_to_task

_OPEN = ("open", "running")


class TaskStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def create(
        self, objective: str, steps: list[Any], *, session_id: UUID | None = None
    ) -> Task:
        """Record a new multi-step task (status 'open'). Steps are numbered 1..N. Each step is either a
        plain description string, or a {"description", "depends_on": [seq,…]} dict for a per-step DAG (§3-6)."""
        async with self.pool.acquire() as conn, conn.transaction():
            task = await conn.fetchrow(
                "INSERT INTO task (session_id, objective) VALUES ($1, $2) RETURNING *",
                session_id, objective)
            step_rows = []
            for i, spec in enumerate(steps, start=1):
                if isinstance(spec, dict):
                    desc = str(spec.get("description", ""))
                    deps = [int(d) for d in (spec.get("depends_on") or [])]
                else:
                    desc, deps = str(spec), []
                step_rows.append(await conn.fetchrow(
                    "INSERT INTO task_step (task_id, seq, description, depends_on) "
                    "VALUES ($1, $2, $3, $4) RETURNING *",
                    task["id"], i, desc, deps))
            await _emit_task(conn, "task.created", task["id"],
                             {"objective": objective, "steps": len(step_rows)})
        return row_to_task(task, step_rows)

    async def checkpoint(self, task_id: UUID, step_seq: int, data: dict[str, Any]) -> None:
        """Save in-step progress so a long step resumes MID-step after a crash (§5), never from scratch.
        Marks the step 'running' and stamps started_at so the resume note shows work is under way."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_step SET checkpoint = $1, "
                "  status = CASE WHEN status IN ('pending','waiting') THEN 'running' ELSE status END, "
                "  started_at = coalesce(started_at, now()) "
                "WHERE task_id = $2 AND seq = $3",
                data, task_id, step_seq)

    async def get(self, task_id: UUID) -> Task | None:
        async with self.pool.acquire() as conn:
            task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            if task is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
        return row_to_task(task, steps)

    async def open_tasks(self, *, limit: int = 5) -> list[Task]:
        """The tasks still worth resuming, most-recently-touched first."""
        async with self.pool.acquire() as conn:
            tasks = await conn.fetch(
                "SELECT * FROM task WHERE status IN ('open', 'running') "
                "ORDER BY updated_at DESC LIMIT $1", limit)
            out = []
            for t in tasks:
                steps = await conn.fetch(
                    "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", t["id"])
                out.append(row_to_task(t, steps))
        return out

    async def current(self) -> Task | None:
        """The single task Sali is most likely working on (the most-recently-touched open one)."""
        tasks = await self.open_tasks(limit=1)
        return tasks[0] if tasks else None

    async def advance(
        self,
        task_id: UUID,
        step_seq: int,
        status: str,
        *,
        note: str | None = None,
        error: str | None = None,
        verified_by: UUID | None = None,
    ) -> Task | None:
        """Set a step's status and re-derive the task's: all steps done/skipped → the task is done,
        otherwise it's running. A failed step leaves the task running (Sali can retry) — only an
        explicit finish() fails or abandons the whole task.

        On a 'failed' step, records an evidence trail the retry driver reads (§8/§9): increments the
        attempt count, stores the last error, and stamps a FailureClass (from ``error`` or, absent that,
        the model's ``note``). On a 'done' step, ``verified_by`` links the tool_execution that verified
        the effect — so "performed" and "verified" are provable separately, never conflated."""
        err_text = error if error is not None else (note if status == "failed" else None)
        failure_class = classify_failure(err_text).value if status == "failed" else None
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE task_step SET status = $1, note = coalesce($2, note), "
                "  started_at = coalesce(started_at, "
                "    CASE WHEN $1 IN ('running','done','failed') THEN now() END), "
                "  finished_at = CASE WHEN $1 IN ('done','failed','skipped') THEN now() "
                "    ELSE finished_at END, "
                "  attempts = attempts + CASE WHEN $1 = 'failed' THEN 1 ELSE 0 END, "
                "  last_error = CASE WHEN $1 = 'failed' THEN $5 ELSE last_error END, "
                "  failure_class = CASE WHEN $1 = 'failed' THEN $6::task_failure_class ELSE failure_class END, "
                "  verified_by = coalesce($7, verified_by), "
                "  verified = CASE WHEN $1 = 'done' AND $7 IS NOT NULL THEN true ELSE verified END "
                "WHERE task_id = $3 AND seq = $4",
                status, note, task_id, step_seq, err_text, failure_class, verified_by)
            statuses = [r["status"] for r in await conn.fetch(
                "SELECT status FROM task_step WHERE task_id = $1", task_id)]
            done = bool(statuses) and all(s in ("done", "skipped") for s in statuses)
            task_status = "done" if done else "running"
            await conn.execute(
                "UPDATE task SET status = $1, updated_at = now() "
                "WHERE id = $2 AND status IN ('open', 'running')",
                task_status, task_id)
            await _emit_task(conn, "task.step_advanced", task_id,
                             {"seq": step_seq, "status": status, "task_status": task_status})
        return await self.get(task_id)

    async def finish(self, task_id: UUID, *, status: str = "done", result: str | None = None) -> None:
        """Explicitly close a task ('done' / 'failed' / 'abandoned') with an optional result note."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task SET status = $1, result = coalesce($2, result), updated_at = now() "
                "WHERE id = $3", status, result, task_id)
            await _emit_task(conn, "task.finished", task_id, {"status": status})


async def _emit_task(conn: Any, event_type: str, task_id: UUID, payload: dict[str, Any]) -> None:
    """Put a task lifecycle change on the durable bus so a live UI can animate task/step progress
    (tasks were previously invisible to the event log). Rides the existing sali_events NOTIFY."""
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) VALUES ($1,'task',$2,$3)",
        event_type, task_id, payload)
