"""ActivityStore (§4/§6) — the unit of work WITHIN a task, with completion discipline.

A task ("build a Laravel app") is pursued through many activities (inspect → research → install → test
→ fix → review). The task survives; the activity changes. The invariant this store enforces is
completion discipline: every started activity must eventually reach a TERMINAL state — completed,
failed, blocked, cancelled, superseded, waiting_for_user, or deferred. There is never a permanent
"started" with no explanation. Activities outlive task cleanup (task_id SET NULL) so the history of
what Sali actually did remains for audit and experience.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

_TERMINAL = frozenset(("completed", "failed", "cancelled", "superseded"))


class ActivityStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def start(
        self, *, task_id: UUID | None, kind: str, description: str | None = None,
        run_id: UUID | None = None, detail: dict[str, Any] | None = None,
    ) -> UUID:
        aid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO activity (id, task_id, run_id, kind, description, detail, status) "
                "VALUES ($1,$2,$3,$4,$5,$6,'started')",
                aid, task_id, run_id, kind, description, detail or {})
        await self._emit("activity.started", task_id, run_id,
                         {"activity_id": str(aid), "kind": kind, "description": (description or "")[:120]})
        return aid

    async def progress(self, activity_id: UUID, *, note: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE activity SET updated_at=now() WHERE id=$1 RETURNING task_id, run_id", activity_id)
        if row is not None:
            await self._emit("activity.progressed", row["task_id"], row["run_id"],
                             {"activity_id": str(activity_id), "note": (note or "")[:160]})

    async def complete(self, activity_id: UUID, *, detail: dict[str, Any] | None = None) -> None:
        await self._terminate(activity_id, "completed", event="activity.completed", detail=detail)

    async def fail(self, activity_id: UUID, *, error: str) -> None:
        await self._terminate(activity_id, "failed", error=error, event="activity.failed")

    async def block(self, activity_id: UUID, *, reason: str) -> None:
        await self._set_status(activity_id, "blocked", error=reason, event="activity.blocked")

    async def wait_for_user(self, activity_id: UUID, *, question: str | None = None) -> None:
        await self._set_status(activity_id, "waiting_for_user", error=question,
                               event="activity.blocked")

    async def defer(self, activity_id: UUID, *, reason: str | None = None) -> None:
        await self._set_status(activity_id, "deferred", error=reason, event="activity.blocked")

    async def cancel(self, activity_id: UUID, *, reason: str | None = None) -> None:
        await self._terminate(activity_id, "cancelled", error=reason, event="activity.completed")

    async def resume(self, activity_id: UUID) -> None:
        """A blocked/deferred/waiting activity becomes active again (§19 interruption→resume)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE activity SET status='started', error=NULL, updated_at=now() "
                "WHERE id=$1 AND status IN ('blocked','deferred','waiting_for_user') "
                "RETURNING task_id, run_id", activity_id)
        if row is not None:
            await self._emit("activity.resumed", row["task_id"], row["run_id"],
                             {"activity_id": str(activity_id)})

    async def current(self, task_id: UUID) -> dict[str, Any] | None:
        """The activity Sali is currently on for a task — the latest non-terminal one, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kind, description, status, error, started_at FROM activity "
                "WHERE task_id=$1 AND status NOT IN ('completed','failed','cancelled','superseded') "
                "ORDER BY started_at DESC LIMIT 1", task_id)
        return dict(row) if row else None

    async def open_activities(self, *, task_id: UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Every non-terminal activity — the completion-discipline worklist (nothing left dangling)."""
        clause = "status NOT IN ('completed','failed','cancelled','superseded')"
        args: list[Any] = []
        if task_id is not None:
            args.append(task_id)
            clause += f" AND task_id=${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, kind, description, status, error, started_at "
                f"FROM activity WHERE {clause} ORDER BY started_at DESC LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def for_task(self, task_id: UUID, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, kind, description, status, error, started_at, completed_at "
                "FROM activity WHERE task_id=$1 ORDER BY started_at LIMIT $2", task_id, limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM activity GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        open_n = sum(v for k, v in by.items() if k not in _TERMINAL)
        return {"open": open_n, "by_status": by}

    async def _terminate(
        self, activity_id: UUID, status: str, *, error: str | None = None, event: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE activity SET status=$2, error=coalesce($3, error), "
                "  detail = detail || $4::jsonb, completed_at=now(), updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('completed','failed','cancelled','superseded') "
                "RETURNING task_id, run_id, kind", activity_id, status, error, detail or {})
        if row is not None:
            await self._emit(event, row["task_id"], row["run_id"],
                             {"activity_id": str(activity_id), "status": status, "kind": row["kind"]})

    async def _set_status(self, activity_id: UUID, status: str, *, error: str | None, event: str) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE activity SET status=$2, error=$3, updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('completed','failed','cancelled','superseded') "
                "RETURNING task_id, run_id", activity_id, status, error)
        if row is not None:
            await self._emit(event, row["task_id"], row["run_id"],
                             {"activity_id": str(activity_id), "status": status})

    async def _emit(self, event_type: str, task_id: UUID | None, run_id: UUID | None,
                    data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id, run_id=run_id,
                                       subject_type="activity", origin="runtime", data=data)
