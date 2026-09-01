"""Decision ledger + task phases (Prompt 6 §30-32).

Both are durable, structured task state that must survive compaction — never left to model prose. The
decision ledger keeps at most one ACTIVE decision per choice (a new decision can supersede an old one,
so there are never two contradictory active decisions). Phases let a very large task progress in stages;
only the current phase plus prior phase summaries normally enter the working context.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.tasks.ledger")


class DecisionStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def record(
        self, task_id: UUID, *, decision: str, reason: str | None = None, source: str = "sali",
        run_id: UUID | None = None, supersedes: UUID | None = None,
    ) -> UUID:
        """Record an ACTIVE decision. If it supersedes a prior decision, that one is marked superseded
        (so contradictory active decisions never coexist, §30). Emits task.decision_created / superseded."""
        did = uuid4()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO task_decision (id, task_id, run_id, decision, reason, source, status) "
                "VALUES ($1,$2,$3,$4,$5,$6,'active')",
                did, task_id, run_id, decision, reason, source)
            if supersedes is not None:
                await conn.execute(
                    "UPDATE task_decision SET status='superseded', superseded_by=$1 "
                    "WHERE id=$2 AND task_id=$3", did, supersedes, task_id)
        await self._emit("task.decision_created", task_id, run_id,
                         {"decision_id": str(did), "decision": decision[:200], "source": source})
        if supersedes is not None:
            await self._emit("task.decision_superseded", task_id, run_id,
                             {"decision_id": str(supersedes), "superseded_by": str(did)})
        return did

    async def active(self, task_id: UUID) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, decision, reason, source, created_at FROM task_decision "
                "WHERE task_id = $1 AND status = 'active' ORDER BY created_at", task_id)
        return [dict(r) for r in rows]

    async def supersede(self, decision_id: UUID, *, by: UUID | None = None,
                        task_id: UUID | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_decision SET status='superseded', superseded_by=$1 WHERE id=$2",
                by, decision_id)
        await self._emit("task.decision_superseded", task_id, None,
                         {"decision_id": str(decision_id), "superseded_by": str(by) if by else None})

    async def _emit(self, event_type: str, task_id: UUID | None, run_id: UUID | None,
                    data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, run_id=run_id, subject_type="task",
                subject_id=task_id, origin="ledger", data=data)


class PhaseStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def start(self, task_id: UUID, name: str, *, summary_of_prior: str | None = None) -> dict[str, Any]:
        """Begin a new phase. Any currently-active phase is completed first (a phase transition, §32),
        carrying the given deterministic summary. Returns the new phase."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE task_phase SET status='done', summary=coalesce($2, summary), completed_at=now() "
                "WHERE task_id=$1 AND status='active'", task_id, summary_of_prior)
            seq = int(await conn.fetchval(
                "SELECT coalesce(max(seq),0)+1 FROM task_phase WHERE task_id=$1", task_id))
            row = await conn.fetchrow(
                "INSERT INTO task_phase (task_id, seq, name, status) VALUES ($1,$2,$3,'active') "
                "RETURNING id, seq, name, status", task_id, seq, name)
        await self._emit("task.phase_started", task_id, {"seq": seq, "name": name})
        return dict(row)

    async def complete(self, task_id: UUID, *, summary: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            done = await conn.fetchrow(
                "UPDATE task_phase SET status='done', summary=coalesce($2, summary), completed_at=now() "
                "WHERE task_id=$1 AND status='active' RETURNING seq, name", task_id, summary)
        if done is not None:
            await self._emit("task.phase_completed", task_id, {"seq": done["seq"], "name": done["name"]})

    async def current(self, task_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT seq, name, status FROM task_phase WHERE task_id=$1 AND status='active' "
                "ORDER BY seq DESC LIMIT 1", task_id)
        return dict(row) if row is not None else None

    async def phases(self, task_id: UUID) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, name, status, summary FROM task_phase WHERE task_id=$1 ORDER BY seq", task_id)
        return [dict(r) for r in rows]

    async def _emit(self, event_type: str, task_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, subject_type="task", subject_id=task_id,
                origin="phase", data=data)
