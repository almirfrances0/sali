"""GoalStore (§5/§6) — durable, hierarchical objectives that outlive a single task.

A goal is not a task: one goal ("maintain my dev environment", "build + deploy the website") can span
many tasks, runs, and interruptions, and it is tagged with its ORIGIN (user / commitment / self /
routine / environment / learned / system) so Sali always knows where an objective came from — an
observation is never silently promoted to a user goal. Goals connect to tasks/commitments/experiences
by soft reference; this is not a second task system (§6/§77).
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

_TERMINAL = frozenset(("completed", "cancelled"))


class GoalStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def create(
        self, *, objective: str, origin: str = "user", priority: int = 5,
        parent_goal: UUID | None = None, task_id: UUID | None = None,
        commitment_id: UUID | None = None, deadline: datetime | None = None,
        success_conditions: list[str] | None = None, next_action: str | None = None,
    ) -> UUID:
        gid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO goal (id, objective, origin, priority, parent_goal, task_id, commitment_id, "
                "  deadline, success_conditions, next_action, status) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open')",
                gid, objective, origin, priority, parent_goal, task_id, commitment_id, deadline,
                success_conditions or [], next_action)
        await self._emit("goal.created", {"goal_id": str(gid), "objective": objective[:120],
                                          "origin": origin, "parent": str(parent_goal) if parent_goal else None})
        return gid

    async def update_progress(self, goal_id: UUID, *, progress: float | None = None,
                              last_action: str | None = None, next_action: str | None = None,
                              status: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE goal SET progress=coalesce($2, progress), last_action=coalesce($3, last_action), "
                "  next_action=coalesce($4, next_action), "
                "  status = coalesce($5, CASE WHEN status='open' THEN 'active' ELSE status END), "
                "  updated_at=now() WHERE id=$1 AND status NOT IN ('completed','cancelled') "
                "RETURNING objective", goal_id, progress, last_action, next_action, status)
        if row is not None:
            await self._emit("goal.updated", {"goal_id": str(goal_id), "progress": progress})

    async def complete(self, goal_id: UUID, *, evidence: dict[str, Any] | None = None) -> None:
        row = await self._settle(goal_id, "completed", evidence=evidence)
        if row is not None:
            await self._emit("goal.completed", {"goal_id": str(goal_id)})

    async def block(self, goal_id: UUID, *, reason: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE goal SET status='blocked', next_action=$2, updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('completed','cancelled')", goal_id, reason)
        await self._emit("goal.blocked", {"goal_id": str(goal_id), "reason": reason[:120]})

    async def cancel(self, goal_id: UUID, *, reason: str) -> None:
        await self._settle(goal_id, "cancelled", evidence={"reason": reason[:200]})

    async def subgoals(self, parent_goal: UUID) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, objective, status, progress, priority FROM goal WHERE parent_goal=$1 "
                "ORDER BY priority, created_at", parent_goal)
        return [dict(r) for r in rows]

    async def active(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The goals Sali is currently pursuing — origin-tagged, best-priority first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, objective, origin, status, priority, progress, deadline, next_action, "
                "  commitment_id, parent_goal FROM goal WHERE status IN ('open','active','blocked') "
                "ORDER BY priority, created_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get(self, goal_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, objective, origin, status, priority, progress, parent_goal, task_id, "
                "  commitment_id, deadline, next_action, success_conditions FROM goal WHERE id=$1", goal_id)
        return dict(row) if row else None

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM goal GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        active = sum(v for k, v in by.items() if k not in _TERMINAL and k != "deferred")
        return {"active": active, "completed": by.get("completed", 0), "by_status": by}

    async def _settle(self, goal_id: UUID, status: str, *, evidence: dict[str, Any] | None) -> Any:
        stamp = ", completed_at=now(), progress=1.0" if status == "completed" else ""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE goal SET status=$2, constraints = constraints || $3::jsonb, updated_at=now()"
                + stamp + " WHERE id=$1 AND status NOT IN ('completed','cancelled') RETURNING objective",
                goal_id, status, {"resolution": evidence} if evidence else {})

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="goal", origin="runtime",
                                       data=data)
