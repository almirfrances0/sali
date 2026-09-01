"""ObligationStore + CommitmentStore (§9/§25/§26/§27) — Sali's unfinished business and his promises.

An OBLIGATION is something Sali started or accepted but hasn't conclusively finished (verify an email,
finish account setup, wait for a deployment). It is bounded (priority + next_check, §28) so persistence
never means "poll everything forever", it resurfaces during cognitive reconstruction, and a
resolved/cancelled obligation stays historically visible (§33).

A COMMITMENT is an enduring responsibility Sali explicitly accepted ("I'll send you the report"). It is
DISTINCT from a task: one commitment can produce many tasks/runs/interruptions, so it has its own
identity and survives conversation compaction (§26). Neither is collapsed into task_id.

Both are durable operational state — they do not disappear when a conversation compacts or the process
restarts, and completion of a commitment still ultimately rests on evidence + the reviewer (§46).
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4


class ObligationStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def create(
        self, *, description: str, source_action: UUID | None = None, object_id: UUID | None = None,
        task_id: UUID | None = None, priority: int = 5, next_action: str | None = None,
        next_check: datetime | None = None, blocking_reason: str | None = None,
    ) -> UUID:
        oid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO obligation (id, source_action, object_id, task_id, description, priority, "
                "  next_action, next_check, blocking_reason) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                oid, source_action, object_id, task_id, description, priority, next_action, next_check,
                blocking_reason)
        await self._emit("obligation.created", task_id,
                         {"obligation_id": str(oid), "description": description[:160],
                          "priority": priority})
        return oid

    async def resolve(self, obligation_id: UUID, *, evidence: dict[str, Any] | None = None) -> None:
        row = await self._settle(obligation_id, "resolved", evidence=evidence)
        if row is not None:
            await self._emit("obligation.resolved", row["task_id"], {"obligation_id": str(obligation_id)})

    async def cancel(self, obligation_id: UUID, *, reason: str) -> None:
        """Explicit abandonment — recorded, never silently erased; the history remains (§33)."""
        row = await self._settle(obligation_id, "cancelled", evidence={"reason": reason[:200]})
        if row is not None:
            await self._emit("obligation.resolved", row["task_id"],
                             {"obligation_id": str(obligation_id), "cancelled": True})

    async def reopen(self, obligation_id: UUID, *, reason: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE obligation SET status='open', resolved_at=NULL, "
                "  blocking_reason=coalesce($2, blocking_reason) WHERE id=$1 RETURNING task_id",
                obligation_id, reason)
        if row is not None:
            await self._emit("obligation.reopened", row["task_id"], {"obligation_id": str(obligation_id)})

    async def block(self, obligation_id: UUID, *, reason: str, next_check: datetime | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE obligation SET status='blocked', blocking_reason=$2, next_check=$3 WHERE id=$1",
                obligation_id, reason[:200], next_check)

    async def mark_checked(self, obligation_id: UUID, *, next_check: datetime | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE obligation SET last_checked=now(), next_check=coalesce($2, next_check) WHERE id=$1",
                obligation_id, next_check)

    async def open(self, *, due_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """Open obligations — Sali's unfinished business, surfaced during cognitive reconstruction (§27).
        due_only restricts to those whose next_check has arrived (bounded attention, §28)."""
        due = " AND (next_check IS NULL OR next_check <= now())" if due_only else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, status, priority, task_id, object_id, source_action, "
                "  blocking_reason, next_action, next_check, created_at FROM obligation "
                f"WHERE status IN ('open','in_progress','blocked'){due} "
                "ORDER BY priority, created_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def for_action(self, source_action: UUID) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, status FROM obligation WHERE source_action=$1", source_action)
        return [dict(r) for r in rows]

    async def get(self, obligation_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, description, status, priority, task_id, object_id, source_action, "
                "  blocking_reason, next_action, evidence FROM obligation WHERE id=$1", obligation_id)
        return dict(row) if row else None

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM obligation GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"open": by.get("open", 0) + by.get("in_progress", 0) + by.get("blocked", 0),
                "by_status": by}

    async def _settle(self, obligation_id: UUID, status: str, *, evidence: dict[str, Any] | None) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE obligation SET status=$2, evidence = evidence || $3::jsonb, resolved_at=now() "
                "WHERE id=$1 AND status IN ('open','in_progress','blocked') RETURNING task_id",
                obligation_id, status, evidence or {})

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id,
                                       subject_type="obligation", origin="runtime", data=data)


class CommitmentStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def create(
        self, *, description: str, task_id: UUID | None = None, deadline: datetime | None = None,
        next_action: str | None = None,
    ) -> UUID:
        cid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO commitment (id, task_id, description, deadline, next_action) "
                "VALUES ($1,$2,$3,$4,$5)", cid, task_id, description, deadline, next_action)
        await self._emit("commitment.created", task_id,
                         {"commitment_id": str(cid), "description": description[:160]})
        return cid

    async def fulfill(self, commitment_id: UUID, *, evidence: dict[str, Any] | None = None) -> None:
        """A commitment is fulfilled on evidence (§46) — the reviewer remains the completion authority
        for the tasks that discharge it; this records the enduring responsibility as met."""
        row = await self._settle(commitment_id, "fulfilled", evidence=evidence, fulfilled=True)
        if row is not None:
            await self._emit("commitment.fulfilled", row["task_id"],
                             {"commitment_id": str(commitment_id)})

    async def cancel(self, commitment_id: UUID, *, reason: str) -> None:
        row = await self._settle(commitment_id, "cancelled", evidence={"reason": reason[:200]})
        if row is not None:
            await self._emit("commitment.cancelled", row["task_id"],
                             {"commitment_id": str(commitment_id)})

    async def advance(self, commitment_id: UUID, status: str, *, next_action: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE commitment SET status=$2, next_action=coalesce($3, next_action), updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('fulfilled','cancelled','expired')",
                commitment_id, status, next_action)

    async def open(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Open commitments — enduring responsibilities that survive compaction/restart (§25)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, status, task_id, deadline, next_action, created_at "
                "FROM commitment WHERE status IN ('open','in_progress','blocked') "
                "ORDER BY created_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get(self, commitment_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, description, status, task_id, deadline, next_action, evidence "
                "FROM commitment WHERE id=$1", commitment_id)
        return dict(row) if row else None

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM commitment GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"open": by.get("open", 0) + by.get("in_progress", 0) + by.get("blocked", 0),
                "fulfilled": by.get("fulfilled", 0), "by_status": by}

    async def _settle(self, commitment_id: UUID, status: str, *, evidence: dict[str, Any] | None,
                      fulfilled: bool = False) -> Any:
        stamp = ", fulfilled_at=now()" if fulfilled else ""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE commitment SET status=$2, evidence = evidence || $3::jsonb, updated_at=now()"
                + stamp + " WHERE id=$1 AND status IN ('open','in_progress','blocked') "
                "RETURNING task_id", commitment_id, status, evidence or {})

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id,
                                       subject_type="commitment", origin="runtime", data=data)
