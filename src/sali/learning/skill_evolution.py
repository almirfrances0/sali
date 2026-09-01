"""Skill evolution (Prompt 7 §14/§15) — a skill improves through a reviewable PROPOSAL, never a blind
rewrite of the Markdown file.

When repeated successful tasks show an approach that diverges from a skill's current guidance, Sali
records a ``skill_proposal`` (current guidance, proposed change, evidence, success/failure counts,
confidence). A human reviews it before it becomes active. The live ``.md`` is never auto-edited, and
existing tasks stay reproducible because every task already snapshots the exact skill content it started
with (``task_skill.content``/``content_hash``, migration 0031) — a task started on version N never
silently behaves like version N+k (§15).
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.learning.skill_evolution")


def _hash(skill_name: str, proposed: str) -> str:
    return hashlib.sha256(f"{skill_name}|{' '.join((proposed or '').lower().split())}".encode()).hexdigest()[:32]


class SkillProposalStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def propose(
        self, *, skill_name: str, proposed_change: str, current_guidance: str | None = None,
        reason: str | None = None, evidence: dict[str, Any] | None = None,
        times_successful: int = 0, times_failed: int = 0, confidence: float = 0.4,
    ) -> UUID:
        """Record (or reinforce) a skill-improvement proposal. Deduped per (skill, proposed change):
        more supporting tasks bump the counters + confidence rather than duplicating."""
        h = _hash(skill_name, proposed_change)
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, times_successful FROM skill_proposal "
                "WHERE skill_name=$1 AND content_hash=$2 AND status NOT IN ('rejected','superseded') LIMIT 1",
                skill_name, h)
            if existing is not None:
                await conn.execute(
                    "UPDATE skill_proposal SET times_successful=times_successful+$2, "
                    "  times_failed=times_failed+$3, confidence=least(0.95, confidence+0.05), "
                    "  updated_at=now() WHERE id=$1", existing["id"], times_successful, times_failed)
                await self._emit("skill.proposal_updated", str(existing["id"]), {"skill": skill_name})
                return UUID(str(existing["id"]))
            pid = uuid4()
            await conn.execute(
                "INSERT INTO skill_proposal (id, skill_name, current_guidance, proposed_change, reason, "
                "  evidence, times_successful, times_failed, confidence, content_hash) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                pid, skill_name, current_guidance, proposed_change, reason, evidence or {},
                times_successful, times_failed, confidence, h)
        await self._emit("skill.proposal_created", str(pid),
                         {"skill": skill_name, "proposed": proposed_change[:200]})
        return pid

    async def accept(self, proposal_id: UUID, *, by: str = "user") -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE skill_proposal SET status='accepted', decided_by=$2, decided_at=now(), "
                "  updated_at=now() WHERE id=$1 AND status='candidate' RETURNING id", proposal_id, by)
        if row is None:
            return False
        await self._emit("skill.proposal_accepted", str(proposal_id), {"by": by})
        return True

    async def reject(self, proposal_id: UUID, *, by: str = "user", reason: str = "") -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE skill_proposal SET status='rejected', decided_by=$2, decided_at=now(), "
                "  updated_at=now() WHERE id=$1 AND status NOT IN ('rejected','superseded') RETURNING id",
                proposal_id, by)
        if row is None:
            return False
        await self._emit("skill.proposal_rejected", str(proposal_id), {"by": by, "reason": reason[:200]})
        return True

    async def pending(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, skill_name, current_guidance, proposed_change, reason, confidence, "
                "  times_successful, times_failed, status, created_at FROM skill_proposal "
                "WHERE status='candidate' ORDER BY confidence DESC, created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def versions(self, skill_name: str) -> list[dict[str, Any]]:
        """The distinct skill snapshots tasks have actually run on (§15 reproducibility) — proof that a
        task's guidance is pinned to the version it started with, derived from the durable task_skill
        snapshots. Each is a content_hash + how many tasks used it + when it was first seen."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content_hash, count(*) AS tasks, min(selected_at) AS first_seen "
                "FROM task_skill WHERE name=$1 GROUP BY content_hash ORDER BY first_seen", skill_name)
        return [dict(r) for r in rows]

    async def _emit(self, event_type: str, proposal_id: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="skill", origin="learning",
                                       data={"proposal_id": proposal_id, **data})
