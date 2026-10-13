"""SideEffectStore (§7/§8/§35/§53) — a durable ledger of consequential actions and their lifecycle.

Sali must never start a consequential multi-step action and silently abandon it. A side effect
(created / modified / deleted / downloaded / installed / sent / registered / …) is tracked
planned → attempted → succeeded | failed | reversed, so a partially-completed workflow stays VISIBLE
and recoverable after interruption or restart. Idempotency is first-class: before repeating an effect,
Sali inspects durable evidence (`already_done`) rather than relying on "I don't remember doing it" (§53).

It complements, and does not duplicate, `task_execution`: where an execution already captures the
evidence, the side effect softly references it via `execution_id` (§8). Targets/states are redacted at
the write boundary so the ledger never becomes a secret dump (§8/§60).
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.security.redact import redact


class SideEffectStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def plan(
        self, *, kind: str, target: str | None = None, task_id: UUID | None = None,
        run_id: UUID | None = None, activity_id: UUID | None = None, execution_id: UUID | None = None,
        before_state: str | None = None, idempotency_key: str | None = None,
    ) -> tuple[UUID, bool]:
        """Record an intended consequential action. Returns (id, already_done). If an effect with the
        same idempotency_key already SUCCEEDED, returns that one with already_done=True — the effect is
        never repeated (§53). Targets/states are redacted (§8)."""
        if idempotency_key is not None:
            done = await self.already_done(idempotency_key)
            if done is not None:
                return UUID(str(done["id"])), True
        sid = uuid4()
        async with self._pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO side_effect (id, task_id, run_id, activity_id, execution_id, kind, "
                    "  target, before_state, idempotency_key, status) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'planned')",
                    sid, task_id, run_id, activity_id, execution_id, kind,
                    redact(target) if target else None,
                    redact(before_state) if before_state else None, idempotency_key)
            except Exception as exc:  # noqa: BLE001 - a concurrent planner with the same key won the race
                if getattr(exc, "sqlstate", None) != "23505" or idempotency_key is None:
                    raise
                existing = await conn.fetchrow(
                    "SELECT id FROM side_effect WHERE idempotency_key=$1 "
                    "  AND status IN ('planned','attempted','succeeded') LIMIT 1", idempotency_key)
                if existing is None:
                    raise
                return UUID(str(existing["id"])), True
        await self._emit("side_effect.planned", task_id, run_id,
                         {"side_effect_id": str(sid), "kind": kind, "target": redact(target or "")[:120]})
        return sid, False

    async def already_done(self, idempotency_key: str) -> dict[str, Any] | None:
        """The succeeded effect for this key, if any — the idempotency check before repeating (§53)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kind, target, status FROM side_effect "
                "WHERE idempotency_key=$1 AND status='succeeded' LIMIT 1", idempotency_key)
        return dict(row) if row else None

    async def attempt(self, side_effect_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE side_effect SET status='attempted' WHERE id=$1 AND status='planned'",
                               side_effect_id)

    async def succeed(self, side_effect_id: UUID, *, after_state: str | None = None,
                      evidence: dict[str, Any] | None = None) -> None:
        row = await self._settle(side_effect_id, "succeeded", after_state=after_state, evidence=evidence)
        if row is not None:
            await self._emit("side_effect.succeeded", row["task_id"], row["run_id"],
                             {"side_effect_id": str(side_effect_id), "kind": row["kind"]})

    async def fail(self, side_effect_id: UUID, *, error: str | None = None,
                   evidence: dict[str, Any] | None = None) -> None:
        ev = {**(evidence or {}), "error": (error or "")[:200]}
        row = await self._settle(side_effect_id, "failed", evidence=ev)
        if row is not None:
            await self._emit("side_effect.failed", row["task_id"], row["run_id"],
                             {"side_effect_id": str(side_effect_id), "kind": row["kind"]})

    async def reverse(self, side_effect_id: UUID, *, note: str | None = None) -> None:
        row = await self._settle(side_effect_id, "reversed", evidence={"note": (note or "")[:200]})
        if row is not None:
            await self._emit("side_effect.recovered", row["task_id"], row["run_id"],
                             {"side_effect_id": str(side_effect_id), "kind": row["kind"]})

    async def unfinished(self, *, task_id: UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Consequential actions still open (planned/attempted) — what a restart must reconcile (§52)."""
        clause = "status IN ('planned','attempted')"
        args: list[Any] = []
        if task_id is not None:
            args.append(task_id)
            clause += f" AND task_id=${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, kind, target, status, created_at "
                f"FROM side_effect WHERE {clause} ORDER BY created_at LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def for_task(self, task_id: UUID, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, kind, target, status, created_at, completed_at FROM side_effect "
                "WHERE task_id=$1 ORDER BY created_at LIMIT $2", task_id, limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM side_effect GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"unfinished": by.get("planned", 0) + by.get("attempted", 0), "by_status": by}

    async def _settle(self, side_effect_id: UUID, status: str, *, after_state: str | None = None,
                      evidence: dict[str, Any] | None = None) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE side_effect SET status=$2, after_state=coalesce($3, after_state), "
                "  evidence = evidence || $4::jsonb, completed_at=now() "
                "WHERE id=$1 AND status IN ('planned','attempted') RETURNING task_id, run_id, kind",
                side_effect_id, status,
                redact(after_state) if after_state else None, evidence or {})

    async def _emit(self, event_type: str, task_id: UUID | None, run_id: UUID | None,
                    data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id, run_id=run_id,
                                       subject_type="side_effect", origin="runtime", data=data)
