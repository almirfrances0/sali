"""DigitalActionStore (§7/§8/§10/§16) — a service interaction as a durable, verifiable lifecycle.

A digital action operates on a DigitalLifeObject at the CAPABILITY level (§14): the planner reasons
"update the account profile"; the mechanism (API / browser / CLI / SSH) is the execution layer's
concern. Its lifecycle never confuses started with completed, or requested with successful (§10/§16):
planned → ready → started → in_progress → awaiting_external_state → verification_required → verified →
maintained, with explicit failure states. When an action reaches awaiting_external_state (e.g. an email
verification), it leaves a durable OPEN OBLIGATION so the unfinished business is never lost (§8). It
becomes 'verified' only on real evidence, and any obligation tied to it is then resolved.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sali.tasks.commitments import ObligationStore

_TERMINAL = frozenset(("verified", "maintained", "failed", "cancelled", "unsupported"))


class DigitalActionStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher
        self._obligations = ObligationStore(pool, publisher)

    async def plan(
        self, *, intent: str, object_id: UUID | None = None, task_id: UUID | None = None,
        run_id: UUID | None = None, capability: str | None = None, target: str | None = None,
        expected_state: str | None = None,
    ) -> UUID:
        aid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO digital_action (id, object_id, task_id, run_id, capability, intent, target, "
                "  expected_state, status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'planned')",
                aid, object_id, task_id, run_id, capability, intent, target, expected_state)
        await self._emit("digital_action.started", task_id,
                         {"action_id": str(aid), "intent": intent[:120], "capability": capability})
        return aid

    async def advance(self, action_id: UUID, status: str, *, observed_state: str | None = None,
                      evidence: dict[str, Any] | None = None) -> None:
        row = await self._update(action_id, status, observed_state=observed_state, evidence=evidence)
        if row is not None:
            await self._emit("digital_action.progressed", row["task_id"],
                             {"action_id": str(action_id), "status": status})

    async def await_external(
        self, action_id: UUID, *, obligation_description: str, next_action: str | None = None,
        next_check: datetime | None = None, priority: int = 5,
    ) -> UUID:
        """The action can't finish now — it needs external state to change (email verification, approval,
        a deployment to come up). Mark it awaiting_external_state and leave a durable OBLIGATION so the
        unfinished business survives compaction/restart and resurfaces later (§8). Returns obligation id."""
        row = await self._update(action_id, "awaiting_external_state")
        task_id = row["task_id"] if row else None
        object_id = row["object_id"] if row else None
        oid = await self._obligations.create(
            description=obligation_description, source_action=action_id, object_id=object_id,
            task_id=task_id, next_action=next_action, next_check=next_check, priority=priority)
        await self._emit("digital_action.awaiting_external_state", task_id,
                         {"action_id": str(action_id), "obligation_id": str(oid)})
        return oid

    async def require_verification(self, action_id: UUID, *, expected_state: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE digital_action SET status='verification_required', "
                "  expected_state=coalesce($2, expected_state), updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('verified','maintained','failed','cancelled')",
                action_id, expected_state)

    async def verify(self, action_id: UUID, *, observed_state: str | None = None,
                     evidence: dict[str, Any] | None = None) -> None:
        """The resulting external state was actually confirmed (§16). Mark verified, complete, and
        resolve any obligation this action created — the unfinished business is now finished with evidence."""
        row = await self._update(action_id, "verified", observed_state=observed_state,
                                 evidence=evidence, complete=True)
        if row is None:
            return
        for ob in await self._obligations.for_action(action_id):
            if ob["status"] in ("open", "in_progress", "blocked"):
                await self._obligations.resolve(UUID(str(ob["id"])), evidence={"verified_action": str(action_id)})
        await self._emit("digital_action.verified", row["task_id"], {"action_id": str(action_id)})

    async def maintained(self, action_id: UUID) -> None:
        await self._update(action_id, "maintained", complete=True)

    async def fail(self, action_id: UUID, *, error: str, status: str = "failed") -> None:
        """Record a failure state (failed / blocked / authentication_required / permission_required /
        unsupported / expired). A failed action stays historically visible and never becomes success (§32)."""
        row = await self._update(action_id, status, error=error,
                                 complete=status in ("failed", "unsupported", "expired"))
        if row is not None:
            await self._emit("digital_action.failed" if status == "failed" else "digital_action.blocked",
                             row["task_id"], {"action_id": str(action_id), "status": status})

    async def resume(self, action_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE digital_action SET status='in_progress', error=NULL, updated_at=now() "
                "WHERE id=$1 AND status IN ('blocked','authentication_required','permission_required',"
                "  'awaiting_external_state') RETURNING task_id", action_id)
        if row is not None:
            await self._emit("digital_action.progressed", row["task_id"],
                             {"action_id": str(action_id), "status": "in_progress", "resumed": True})

    async def unfinished(self, *, object_id: UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Open digital actions — what a restart must reconcile; never silently abandoned (§32/§45)."""
        clause = ("status NOT IN ('verified','maintained','failed','cancelled','unsupported')")
        args: list[Any] = []
        if object_id is not None:
            args.append(object_id)
            clause += f" AND object_id=${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, object_id, task_id, capability, intent, status, expected_state, created_at "
                f"FROM digital_action WHERE {clause} ORDER BY created_at LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def get(self, action_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, object_id, task_id, capability, intent, target, status, expected_state, "
                "  observed_state, evidence, error FROM digital_action WHERE id=$1", action_id)
        return dict(row) if row else None

    async def for_object(self, object_id: UUID, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, capability, intent, status, created_at, completed_at FROM digital_action "
                "WHERE object_id=$1 ORDER BY created_at LIMIT $2", object_id, limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM digital_action GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        unfinished = sum(v for k, v in by.items() if k not in _TERMINAL)
        return {"unfinished": unfinished, "by_status": by}

    async def _update(self, action_id: UUID, status: str, *, observed_state: str | None = None,
                      error: str | None = None, evidence: dict[str, Any] | None = None,
                      complete: bool = False) -> Any:
        stamp = ", completed_at=now()" if complete else ""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE digital_action SET status=$2, observed_state=coalesce($3, observed_state), "
                "  error=coalesce($4, error), evidence = evidence || $5::jsonb, updated_at=now()" + stamp
                + " WHERE id=$1 AND status NOT IN ('verified','maintained','failed','cancelled') "
                "RETURNING task_id, object_id", action_id, status, observed_state, error, evidence or {})

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id,
                                       subject_type="digital_action", origin="runtime", data=data)
