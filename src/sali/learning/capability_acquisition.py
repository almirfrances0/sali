"""CapabilityAcquisitionStore (§5/§10/§53/§68.10-18) — the durable "acquire a missing capability" loop.

When Sali finds he lacks a capability, the attempt to acquire it is durable, resumable state:
gap_identified → researching → acquiring → verifying → acquired, or failed / blocked. It survives
interruption, compaction, and restart, and is never restarted from zero (§60/§61/§62). A FAILED
acquisition stays resumable and never itself becomes a claimed capability — the capability only becomes
real via CapabilityStore.record_attempt on verified evidence (§3/§42/§59). This store tracks the
*process*; the CapabilityStore tracks the *result*.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4


class CapabilityAcquisitionStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def identify_gap(
        self, *, capability_name: str, required: list[str] | None = None,
        missing: list[str] | None = None, task_id: UUID | None = None, run_id: UUID | None = None,
        scope: str = "environment", scope_ref: str | None = None, notes: str | None = None,
    ) -> UUID:
        """Record a capability gap Sali intends to close. Re-identifying the same gap RESUMES the live
        acquisition rather than duplicating it (§53)."""
        existing = await self.active(capability_name=capability_name, scope=scope, scope_ref=scope_ref)
        if existing is not None:
            return UUID(str(existing["id"]))
        aid = uuid4()
        gap = {"required": required or [], "missing": missing or [capability_name]}
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO capability_acquisition (id, capability_name, task_id, run_id, scope, "
                "  scope_ref, status, gap, notes) VALUES ($1,$2,$3,$4,$5,$6,'gap_identified',$7,$8)",
                aid, capability_name, task_id, run_id, scope, scope_ref, gap, notes)
        await self._emit("capability.discovered", task_id,
                         {"acquisition_id": str(aid), "capability": capability_name,
                          "missing": gap["missing"]})
        return aid

    async def advance(self, acquisition_id: UUID, status: str, *, notes: str | None = None) -> None:
        """Move the acquisition forward (researching → acquiring → verifying). Deterministic lifecycle."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE capability_acquisition SET status=$2, notes=coalesce($3, notes), updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('acquired','failed') RETURNING task_id, capability_name",
                acquisition_id, status, notes)
        if row is not None:
            await self._emit("capability.attempted", row["task_id"],
                             {"acquisition_id": str(acquisition_id), "capability": row["capability_name"],
                              "status": status})

    async def acquired(self, acquisition_id: UUID, *, capability_name: str | None = None) -> None:
        """Close the gap. `capability_name` renames the row as it settles: the gap is opened before the
        work is done, when the reusable skill has no name yet, and only the finished work reveals what
        the skill actually was. Renaming and settling in ONE statement keeps the live-acquisition unique
        index satisfied — a settled row is outside that partial index."""
        row = await self._settle(acquisition_id, "acquired", rename=capability_name)
        if row is not None:
            await self._emit("capability.verified", row["task_id"],
                             {"acquisition_id": str(acquisition_id), "capability": row["capability_name"]})

    async def fail(self, acquisition_id: UUID, *, error: str) -> None:
        row = await self._settle(acquisition_id, "failed", error=error)
        if row is not None:
            await self._emit("capability.failed", row["task_id"],
                             {"acquisition_id": str(acquisition_id), "capability": row["capability_name"]})

    async def block(self, acquisition_id: UUID, *, reason: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE capability_acquisition SET status='blocked', error=$2, updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('acquired','failed')", acquisition_id, reason[:200])

    async def active(self, *, capability_name: str, scope: str = "environment",
                     scope_ref: str | None = None) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, gap, notes FROM capability_acquisition "
                "WHERE capability_name=$1 AND scope=$2 AND coalesce(scope_ref,'')=coalesce($3,'') "
                "  AND status NOT IN ('acquired','failed') ORDER BY created_at DESC LIMIT 1",
                capability_name, scope, scope_ref)
        return dict(row) if row else None

    async def for_task(self, task_id: UUID) -> dict[str, Any] | None:
        """The live acquisition this task is working through, found by TASK rather than by name. The
        name is not a stable handle across the arc — it is provisional until the work succeeds — but the
        task id is, so every transition (acquiring → verifying → acquired/failed) resolves through here."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, capability_name, status FROM capability_acquisition "
                "WHERE task_id=$1 AND status NOT IN ('acquired','failed') "
                "ORDER BY created_at DESC LIMIT 1", task_id)
        return dict(row) if row else None

    async def get(self, acquisition_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, capability_name, task_id, status, gap, notes, error, scope_ref "
                "FROM capability_acquisition WHERE id=$1", acquisition_id)
        return dict(row) if row else None

    async def open(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Acquisitions still in progress — what a restart must resume (§62)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, capability_name, task_id, status, gap, created_at FROM capability_acquisition "
                "WHERE status IN ('gap_identified','researching','acquiring','verifying','blocked') "
                "ORDER BY created_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, count(*) AS n FROM capability_acquisition GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        in_progress = sum(v for k, v in by.items() if k not in ("acquired", "failed"))
        return {"in_progress": in_progress, "acquired": by.get("acquired", 0), "by_status": by}

    async def _settle(self, acquisition_id: UUID, status: str, *, error: str | None = None,
                      rename: str | None = None) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE capability_acquisition SET status=$2, error=coalesce($3, error), "
                "  capability_name=coalesce($4, capability_name), "
                "  completed_at=now(), updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('acquired','failed') RETURNING task_id, capability_name",
                acquisition_id, status, error, rename)

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id,
                                       subject_type="capability", origin="learning", data=data)
