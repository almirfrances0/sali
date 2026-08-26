"""TaskStore — persist and advance multi-step tasks (spec §24).

A task and its steps live in the datastore, so they survive process restarts: the loop reads the
open tasks back into context each turn and Sali resumes. The store keeps the task's own status in
sync with its steps (all steps done → the task is done), so "is this finished?" is never guessed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.tasks.models import Task, row_to_task

_OPEN = ("open", "running")


class TaskStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def create(
        self, objective: str, steps: list[str], *, session_id: UUID | None = None
    ) -> Task:
        """Record a new multi-step task (status 'open'). Steps are numbered 1..N in order."""
        async with self.pool.acquire() as conn, conn.transaction():
            task = await conn.fetchrow(
                "INSERT INTO task (session_id, objective) VALUES ($1, $2) RETURNING *",
                session_id, objective)
            step_rows = []
            for i, desc in enumerate(steps, start=1):
                step_rows.append(await conn.fetchrow(
                    "INSERT INTO task_step (task_id, seq, description) VALUES ($1, $2, $3) RETURNING *",
                    task["id"], i, str(desc)))
        return row_to_task(task, step_rows)

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
        self, task_id: UUID, step_seq: int, status: str, *, note: str | None = None
    ) -> Task | None:
        """Set a step's status and re-derive the task's: all steps done/skipped → the task is done,
        otherwise it's running. A failed step leaves the task running (Sali can retry) — only an
        explicit finish() fails or abandons the whole task."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE task_step SET status = $1, note = coalesce($2, note), "
                "  started_at = coalesce(started_at, "
                "    CASE WHEN $1 IN ('running','done','failed') THEN now() END), "
                "  finished_at = CASE WHEN $1 IN ('done','failed','skipped') THEN now() "
                "    ELSE finished_at END "
                "WHERE task_id = $3 AND seq = $4",
                status, note, task_id, step_seq)
            statuses = [r["status"] for r in await conn.fetch(
                "SELECT status FROM task_step WHERE task_id = $1", task_id)]
            done = bool(statuses) and all(s in ("done", "skipped") for s in statuses)
            await conn.execute(
                "UPDATE task SET status = $1, updated_at = now() "
                "WHERE id = $2 AND status IN ('open', 'running')",
                "done" if done else "running", task_id)
        return await self.get(task_id)

    async def finish(self, task_id: UUID, *, status: str = "done", result: str | None = None) -> None:
        """Explicitly close a task ('done' / 'failed' / 'abandoned') with an optional result note."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task SET status = $1, result = coalesce($2, result), updated_at = now() "
                "WHERE id = $3", status, result, task_id)
