"""InitiativeStore (§9-§12) — durable initiative candidates and their lifecycle.

An initiative is Sali noticing that something might be worth doing. It is durable (survives compaction /
restart), deduped by (source, subject_ref) so the same opportunity is never re-created every cycle
(§14), and carries STRUCTURED scoring metadata (priority/urgency/risk/confidence + reason codes) rather
than chain-of-thought (§10). Its lifecycle keeps "I should look into this" strictly separate from "I
changed something" (§12): observed → candidate → evaluated → planned → ready → executing → verifying →
completed, or dismissed / deferred / blocked / expired. attempts + next_attempt implement backoff so a
repeatedly-failing initiative doesn't burn the GPU forever (§40). An initiative is never authority to act
— the authority model decides that separately (§53).
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

_TERMINAL = frozenset(("completed", "dismissed", "expired"))


class InitiativeStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def upsert_candidate(
        self, *, source: str, subject_ref: str, title: str, priority_score: float = 0.0,
        urgency_score: float = 0.0, risk_score: float = 0.0, confidence: float = 0.5,
        reason_codes: list[str] | None = None, goal_id: UUID | None = None,
        commitment_id: UUID | None = None, evidence: dict[str, Any] | None = None,
    ) -> tuple[UUID, bool]:
        """Create or refresh an initiative candidate. Deduped by (source, subject_ref): a still-live
        initiative for the same opportunity is UPDATED (scores refreshed), never duplicated. Returns
        (id, created)."""
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM initiative WHERE source=$1 AND subject_ref=$2 "
                "  AND status NOT IN ('completed','dismissed','expired') LIMIT 1", source, subject_ref)
            if existing is not None:
                await conn.execute(
                    "UPDATE initiative SET priority_score=$2, urgency_score=$3, risk_score=$4, "
                    "  confidence=$5, reason_codes=$6, updated_at=now() WHERE id=$1",
                    existing["id"], priority_score, urgency_score, risk_score, confidence,
                    reason_codes or [])
                return UUID(str(existing["id"])), False
            iid = uuid4()
            await conn.execute(
                "INSERT INTO initiative (id, source, subject_ref, title, status, priority_score, "
                "  urgency_score, risk_score, confidence, reason_codes, goal_id, commitment_id, evidence) "
                "VALUES ($1,$2,$3,$4,'candidate',$5,$6,$7,$8,$9,$10,$11,$12)",
                iid, source, subject_ref, title, priority_score, urgency_score, risk_score, confidence,
                reason_codes or [], goal_id, commitment_id, evidence or {})
        await self._emit("life.initiative.created", {"initiative_id": str(iid), "source": source,
                                                     "title": title[:120], "priority": priority_score})
        return iid, True

    async def advance(self, initiative_id: UUID, status: str) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE initiative SET status=$2, updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('completed','dismissed','expired') RETURNING title, source",
                initiative_id, status)
        if row is not None and status in ("ready", "executing"):
            await self._emit("life.initiative.selected", {"initiative_id": str(initiative_id),
                                                          "status": status, "title": row["title"][:120]})

    async def complete(self, initiative_id: UUID, *, evidence: dict[str, Any] | None = None) -> None:
        row = await self._settle(initiative_id, "completed", evidence=evidence)
        if row is not None:
            await self._emit("life.initiative.completed", {"initiative_id": str(initiative_id)})

    async def dismiss(self, initiative_id: UUID, *, reason: str) -> None:
        await self._settle(initiative_id, "dismissed", evidence={"reason": reason[:200]})

    async def defer(self, initiative_id: UUID, *, next_attempt: datetime | None = None,
                    reason: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE initiative SET status='deferred', next_attempt=$2, "
                "  evidence = evidence || $3::jsonb, updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('completed','dismissed','expired')",
                initiative_id, next_attempt, {"defer_reason": reason} if reason else {})
        await self._emit("life.initiative.deferred", {"initiative_id": str(initiative_id)})

    async def block(self, initiative_id: UUID, *, reason: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE initiative SET status='blocked', evidence = evidence || $2::jsonb, updated_at=now() "
                "WHERE id=$1 AND status NOT IN ('completed','dismissed','expired')",
                initiative_id, {"blocked_reason": reason[:200]})
        await self._emit("life.initiative.blocked", {"initiative_id": str(initiative_id)})

    async def record_attempt(self, initiative_id: UUID, *, success: bool,
                             next_attempt: datetime | None = None) -> int:
        """An attempt was made. Bumps the attempt count (for backoff) and, on failure, sets next_attempt
        so a repeatedly-failing initiative backs off instead of looping (§40). Returns the new count."""
        async with self._pool.acquire() as conn:
            n = await conn.fetchval(
                "UPDATE initiative SET attempts = attempts + 1, next_attempt=$2, updated_at=now() "
                "WHERE id=$1 RETURNING attempts", initiative_id, next_attempt if not success else None)
        return int(n or 0)

    async def top(self, *, limit: int = 5, ready_only: bool = False) -> list[dict[str, Any]]:
        """The highest-priority open initiatives whose backoff window has elapsed — the worklist (§9)."""
        statuses = "('candidate','evaluated','planned','ready')" if not ready_only else "('ready')"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, source, subject_ref, title, status, priority_score, urgency_score, "
                "  risk_score, confidence, reason_codes, attempts, goal_id, commitment_id "
                f"FROM initiative WHERE status IN {statuses} "
                "  AND (next_attempt IS NULL OR next_attempt <= now()) "
                "ORDER BY priority_score DESC, urgency_score DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def by_subject(self, *, source: str, subject_ref: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, attempts, priority_score FROM initiative "
                "WHERE source=$1 AND subject_ref=$2 ORDER BY created_at DESC LIMIT 1", source, subject_ref)
        return dict(row) if row else None

    async def get(self, initiative_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, source, subject_ref, title, status, priority_score, urgency_score, "
                "  risk_score, confidence, reason_codes, attempts, goal_id FROM initiative WHERE id=$1",
                initiative_id)
        return dict(row) if row else None

    async def open(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, source, subject_ref, title, status, priority_score, attempts, created_at "
                "FROM initiative WHERE status NOT IN ('completed','dismissed','expired') "
                "ORDER BY priority_score DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM initiative GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        openn = sum(v for k, v in by.items() if k not in _TERMINAL)
        return {"open": openn, "by_status": by}

    async def _settle(self, initiative_id: UUID, status: str, *, evidence: dict[str, Any] | None) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "UPDATE initiative SET status=$2, evidence = evidence || $3::jsonb, resolved_at=now(), "
                "  updated_at=now() WHERE id=$1 AND status NOT IN ('completed','dismissed','expired') "
                "RETURNING title", initiative_id, status, evidence or {})

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="initiative", origin="background",
                                       data=data)
