"""OpenLoopStore — durable "unresolved matters Sali is holding mental space for" (§6).

Distinct from every other domain object:

* A **task** is formal work with a plan + steps + review.
* A **goal** is an aspiration ("build a portfolio site").
* A **commitment** is something promised, usually with a deadline.
* An **initiative** is a candidate ACTION scored by the InitiativeEngine.
* An **open loop** is the mid-density state BEFORE any of the above: "I noticed X and haven't
  resolved it yet." Sali carries it in his agenda; when the InitiativeEngine cycles, an open
  loop with high priority becomes an initiative candidate (via source='open_loop').

Lifecycle: open → investigating → resolved | dismissed | expired. `last_touched_at` moves each
time Sali revisits (adjusts priority, reads it in context, mentions it in conversation). A loop
that hasn't been touched past `expires_at` decays to 'expired' on the next consolidation.

**Not a memory row and not a note**: memory stores facts; a note is a chat artefact. An open
loop is Sali's own working set — the "things still on my mind" list.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.tasks.open_loops")


VALID_KINDS = ("noticed", "question", "investigation", "promise_followup",
               "idea", "verification", "monitoring")
VALID_STATUSES = ("open", "investigating", "resolved", "dismissed", "expired")


@dataclass(slots=True)
class OpenLoop:
    id: UUID
    title: str
    description: str
    kind: str
    source: str
    source_ref: str | None
    priority: float
    goal_id: UUID | None
    task_id: UUID | None
    status: str
    resolution: str | None
    last_touched_at: datetime
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id), "title": self.title, "description": self.description,
            "kind": self.kind, "source": self.source, "source_ref": self.source_ref,
            "priority": self.priority,
            "goal_id": str(self.goal_id) if self.goal_id else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "status": self.status, "resolution": self.resolution,
            "last_touched_at": self.last_touched_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class OpenLoopStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def open_or_reinforce(
        self, *, title: str, description: str = "", kind: str = "noticed",
        source: str = "chat", source_ref: str | None = None,
        priority: float = 0.4, goal_id: UUID | None = None, task_id: UUID | None = None,
        ttl_days: int | None = 14,
    ) -> UUID:
        """Create a new open loop OR reinforce an existing one with the same title (case-fold).

        Dedup key: `LOWER(title)`. A repeated "noticed X" bumps `last_touched_at` and nudges
        priority up rather than duplicating. `ttl_days` sets an `expires_at`; None = never.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown kind: {kind}")
        expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)) if ttl_days else None
        priority = max(0.0, min(1.0, priority))

        async with self._pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, priority FROM sali.open_loop "
                "WHERE LOWER(title) = LOWER($1) AND status IN ('open', 'investigating') "
                "LIMIT 1", title)
            if existing is not None:
                # Reinforcement: touch + nudge priority up (bounded by 1.0). Don't overwrite the
                # description — the original wording is authoritative until an explicit update().
                new_priority = min(1.0, float(existing["priority"]) + 0.05)
                await conn.execute(
                    "UPDATE sali.open_loop SET priority=$2, last_touched_at=now(), "
                    "updated_at=now() WHERE id=$1", existing["id"], new_priority)
                loop_id = UUID(str(existing["id"]))
                await self._emit("open_loop.reinforced", loop_id,
                                 {"title": title[:120], "priority": new_priority})
                return loop_id
            loop_id = uuid4()
            await conn.execute(
                "INSERT INTO sali.open_loop "
                "(id, title, description, kind, source, source_ref, priority, "
                " goal_id, task_id, expires_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                loop_id, title, description, kind, source, source_ref, priority,
                goal_id, task_id, expires_at)
            await self._emit("open_loop.created", loop_id,
                             {"title": title[:120], "kind": kind, "priority": priority})
            return loop_id

    async def touch(self, loop_id: UUID, *, priority_bump: float = 0.05) -> bool:
        """Record that Sali revisited this loop (e.g. it appeared in an initiative cycle or was
        referenced in context). Nudges priority up unless already at 1.0."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE sali.open_loop "
                "SET priority = LEAST(1.0, priority + $2), last_touched_at = now(), "
                "    updated_at = now() "
                "WHERE id = $1 AND status IN ('open', 'investigating') RETURNING id",
                loop_id, priority_bump)
        return row is not None

    async def resolve(self, loop_id: UUID, *, resolution: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE sali.open_loop SET status='resolved', resolution=$2, "
                "resolved_at=now(), updated_at=now() "
                "WHERE id=$1 AND status IN ('open','investigating') RETURNING id",
                loop_id, resolution[:500])
        if row is None:
            return False
        await self._emit("open_loop.resolved", loop_id,
                         {"resolution": resolution[:200]})
        return True

    async def dismiss(self, loop_id: UUID, *, reason: str = "") -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE sali.open_loop SET status='dismissed', resolution=$2, "
                "resolved_at=now(), updated_at=now() "
                "WHERE id=$1 AND status IN ('open','investigating') RETURNING id",
                loop_id, reason[:500])
        if row is None:
            return False
        await self._emit("open_loop.dismissed", loop_id, {"reason": reason[:200]})
        return True

    async def open_loops(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Currently-open loops, highest priority first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, description, kind, source, source_ref, priority, "
                "goal_id, task_id, status, resolution, last_touched_at, expires_at, "
                "created_at, updated_at, resolved_at "
                "FROM sali.open_loop "
                "WHERE status IN ('open', 'investigating') "
                "ORDER BY priority DESC, last_touched_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def expire_stale(self) -> int:
        """Move loops past `expires_at` (with no recent touch) to 'expired'. Returns the count.
        Called from the consolidation cycle."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "WITH updated AS ("
                "  UPDATE sali.open_loop "
                "  SET status='expired', updated_at=now() "
                "  WHERE status IN ('open','investigating') "
                "    AND expires_at IS NOT NULL AND expires_at < now() "
                "    AND last_touched_at < now() - INTERVAL '1 day' "
                "  RETURNING id) SELECT count(*) FROM updated")
        n = int(row or 0)
        if n:
            log.info("open_loop_expired", count=n)
        return n

    async def as_initiative_candidates(self, limit: int = 6) -> list[dict[str, Any]]:
        """Adapter for `InitiativeEngine.generate_candidates`. Returns the shape the initiative
        loop uses (id, title, priority, source_ref). Includes only high-enough priority so the
        InitiativeEngine's budget isn't dominated by low-priority noticed items."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, priority, kind FROM sali.open_loop "
                "WHERE status IN ('open','investigating') AND priority >= 0.5 "
                "ORDER BY priority DESC, last_touched_at DESC LIMIT $1", limit)
        return [{"id": r["id"], "title": r["title"], "priority": float(r["priority"]),
                 "kind": r["kind"]} for r in rows]

    async def _emit(self, event_type: str, loop_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, subject_type="open_loop", subject_id=loop_id,
                origin="background", data=data)
