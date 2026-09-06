"""CuriosityStore — evidence-tracked knowledge gaps (§5).

A curiosity is NOT a proposed lesson (that's `learning_candidate`), NOT a preference (that's
`behavior_proposal`), NOT a task (that's `sali.task`). It's:

    "I keep encountering this and I don't understand it well."

Curiosities have:
* a subject slug (unique per active curiosity — repeated encounters reinforce a single row)
* times_encountered + last_encountered_at (rising volume is one signal for priority)
* current_understanding (updated as Sali learns)
* discoveries (a small append-only JSONB log of what Sali found)
* remaining_interesting (periodic re-evaluation whether the gap still matters)

They exist to give the InitiativeEngine somewhere to look when it asks "is there low-risk
idle-time work worth doing?" — a high-priority curiosity is a legitimate reason to spend
background research budget.

Deliberately NOT autonomous by itself: creating a curiosity doesn't schedule anything. The
InitiativeEngine reads them (source='curiosity') and the reviewer/policy layers still gate
actual research. Fake autonomy is prohibited: a curiosity that never gets investigated stays
open, and after `expires_at` it decays quietly (§34 decay).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.learning.curiosity")


VALID_STATUSES = ("open", "investigating", "resolved", "archived")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(subject: str) -> str:
    """Deterministic slug for dedup (LOWER, non-alphanum → dash, capped 80)."""
    s = _SLUG_RE.sub("-", subject.lower()).strip("-")
    return s[:80] if s else "unnamed"


@dataclass(slots=True)
class Curiosity:
    id: UUID
    subject: str
    statement: str
    why_it_matters: str
    current_understanding: str
    priority: float
    times_encountered: int
    last_encountered_at: datetime
    discoveries: list[dict[str, Any]]
    status: str
    remaining_interesting: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class CuriosityStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def encounter(
        self, *, subject: str, statement: str, why_it_matters: str = "",
        priority: float = 0.4, ttl_days: int | None = 45,
    ) -> UUID:
        """Record that Sali encountered a knowledge gap. Dedups per subject slug: the same
        subject repeatedly encountered reinforces one row (bumps times_encountered + priority)
        rather than creating a new curiosity every time."""
        slug = _slug(subject)
        priority = max(0.0, min(1.0, priority))
        expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)) if ttl_days else None

        async with self._pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, priority, times_encountered FROM sali.curiosity "
                "WHERE subject = $1 AND status IN ('open', 'investigating')", slug)
            if existing is not None:
                new_pri = min(1.0, float(existing["priority"]) + 0.03)
                new_count = int(existing["times_encountered"]) + 1
                await conn.execute(
                    "UPDATE sali.curiosity "
                    "SET priority=$2, times_encountered=$3, "
                    "    last_encountered_at=now(), updated_at=now() "
                    "WHERE id=$1", existing["id"], new_pri, new_count)
                cid = UUID(str(existing["id"]))
                await self._emit("curiosity.reinforced", cid, {
                    "subject": slug, "times_encountered": new_count, "priority": new_pri})
                return cid
            cid = uuid4()
            await conn.execute(
                "INSERT INTO sali.curiosity "
                "(id, subject, statement, why_it_matters, priority, expires_at) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                cid, slug, statement, why_it_matters, priority, expires_at)
            await self._emit("curiosity.created", cid, {
                "subject": slug, "statement": statement[:200], "priority": priority})
            return cid

    async def learn(self, curiosity_id: UUID, *, note: str,
                    new_understanding: str | None = None) -> bool:
        """Append a discovery to the curiosity's log; optionally update its understanding.
        Does NOT auto-close — Sali or Almir decides when a gap is genuinely resolved."""
        entry = {"at": datetime.now(timezone.utc).isoformat(), "note": note[:500]}
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE sali.curiosity "
                "SET discoveries = discoveries || $2::jsonb, "
                "    current_understanding = COALESCE($3, current_understanding), "
                "    updated_at = now() "
                "WHERE id = $1 AND status IN ('open', 'investigating') RETURNING id",
                curiosity_id, [entry], new_understanding)
        if row is None:
            return False
        await self._emit("curiosity.learned", curiosity_id, {"note": note[:200]})
        return True

    async def resolve(self, curiosity_id: UUID, *, conclusion: str) -> bool:
        entry = {"at": datetime.now(timezone.utc).isoformat(),
                 "note": f"[resolved] {conclusion[:400]}"}
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE sali.curiosity SET status='resolved', resolved_at=now(), "
                "  updated_at=now(), discoveries = discoveries || $2::jsonb "
                "WHERE id = $1 AND status IN ('open', 'investigating') RETURNING id",
                curiosity_id, [entry])
        if row is None:
            return False
        await self._emit("curiosity.resolved", curiosity_id, {"conclusion": conclusion[:200]})
        return True

    async def re_evaluate(self, curiosity_id: UUID, *, still_interesting: bool) -> None:
        """Periodic consolidation: mark whether the gap still matters. When flipped false,
        the next expiry sweep archives it."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sali.curiosity SET remaining_interesting=$2, updated_at=now() "
                "WHERE id = $1", curiosity_id, still_interesting)

    async def open_curiosities(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Currently-open curiosities, sorted by priority × recency-of-encounter."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, subject, statement, why_it_matters, current_understanding, "
                "priority, times_encountered, last_encountered_at, discoveries, status, "
                "remaining_interesting, expires_at, created_at, updated_at, resolved_at "
                "FROM sali.curiosity WHERE status IN ('open','investigating') "
                "ORDER BY priority DESC, times_encountered DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def archive_stale(self) -> int:
        """Archive curiosities past `expires_at` where remaining_interesting is false."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "WITH updated AS ("
                "  UPDATE sali.curiosity SET status='archived', updated_at=now() "
                "  WHERE status IN ('open','investigating') "
                "    AND expires_at IS NOT NULL AND expires_at < now() "
                "    AND remaining_interesting = false "
                "  RETURNING id) SELECT count(*) FROM updated")
        n = int(row or 0)
        if n:
            log.info("curiosity_archived", count=n)
        return n

    async def as_initiative_candidates(self, limit: int = 3) -> list[dict[str, Any]]:
        """Adapter for InitiativeEngine — high-priority curiosities become 'investigate' initiative
        candidates. Kept small (default 3) so background research budget isn't dominated by
        idle-time curiosity work over real user obligations."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, subject, statement, priority, times_encountered "
                "FROM sali.curiosity "
                "WHERE status IN ('open','investigating') AND priority >= 0.6 "
                "ORDER BY priority DESC, times_encountered DESC LIMIT $1", limit)
        return [{"id": r["id"], "subject": r["subject"], "statement": r["statement"],
                 "priority": float(r["priority"]),
                 "times_encountered": int(r["times_encountered"])} for r in rows]

    async def _emit(self, event_type: str, curiosity_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, subject_type="curiosity", subject_id=curiosity_id,
                origin="background", data=data)
