"""Behavioral learning (Prompt 7 §6/§7/§16/§19/§42) — user feedback and repeated patterns become
reviewable behavior PROPOSALS, never automatic mutations.

Knowledge is "Laravel uses Artisan for migrations"; behavior is "prefer the project's existing
conventions before introducing new architecture." User feedback ("always use PostgreSQL", "ask me
first", "that was wrong") is strong evidence, so it becomes a candidate immediately — but a candidate
affects future planning only once ACCEPTED, and a change to core behavior needs the user's approval
(§42). Scope is preserved, so a project preference never silently becomes global (§8). Nothing here can
alter a core invariant — behavior proposals are inputs to planning, not to the kernel (§16/§17/§37).
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.learning.behavior")


@dataclass(slots=True)
class BehaviorObservation:
    """A structured behavioral signal extracted from a user message (§6). Deterministic — no model."""

    trigger: str
    proposed_behavior: str
    sentiment: str          # 'directive' | 'preference' | 'prohibition' | 'correction' | 'praise'
    scope: str = "user"


# Deterministic feedback patterns (§6). Ordered — first match wins. Each captures the operative clause.
_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bnext time\s+(?:please\s+)?ask me\b.*", "prohibition", "ask the user before: {rest}"),
    (r"\bask me (?:first|before)\b.*", "prohibition", "ask the user before: {rest}"),
    (r"\b(?:don'?t|do not|never)\s+(.+?)(?:\s+again)?[.!]*$", "prohibition", "avoid: {g1}"),
    (r"\bstop\s+(.+?)[.!]*$", "prohibition", "stop: {g1}"),
    (r"\balways\s+(.+?)[.!]*$", "directive", "always: {g1}"),
    (r"\bi(?:'d)?\s*(?:prefer|like|want)\s+(.+?)[.!]*$", "preference", "prefer: {g1}"),
    (r"\bplease\s+(?:use|prefer)\s+(.+?)[.!]*$", "preference", "prefer: {g1}"),
    (r"\bthat(?:'s| is| was)?\s+(?:wrong|incorrect|not right)\b.*", "correction",
     "the previous approach was rejected by the user — reconsider it"),
    (r"\b(?:good|perfect|great|exactly)\b.*(?:wanted|right|correct)?.*", "praise",
     "the previous approach was approved by the user"),
)


def classify_feedback(message: str) -> BehaviorObservation | None:
    """Turn an explicit user instruction/reaction into a structured behavioral observation (§6). Returns
    None when the message carries no behavioral signal — most turns produce nothing (no spam, §29)."""
    text = (message or "").strip()
    if not text or len(text) > 600:
        return None
    low = text.lower()
    for pat, sentiment, template in _PATTERNS:
        m = re.search(pat, low)
        if not m:
            continue
        g1 = (m.group(1).strip() if m.groups() and m.group(1) else "")
        rest = low[m.start():].strip()
        proposed = template.format(g1=g1, rest=rest)[:280]
        trigger = g1 or rest[:120] or "general"
        return BehaviorObservation(trigger=trigger[:200], proposed_behavior=proposed,
                                   sentiment=sentiment)
    return None


@dataclass(slots=True)
class BehaviorProfile:
    """A DERIVED behavioral profile (§19) — reconstructed from durable records, never a hand-maintained
    blob. What Sali has learned about how to work, from accepted proposals + verified experience."""

    communication_style: str = ""
    tool_preferences: list[str] = field(default_factory=list)
    workflow_preferences: list[str] = field(default_factory=list)
    known_user_preferences: list[str] = field(default_factory=list)
    project_preferences: list[str] = field(default_factory=list)
    successful_patterns: list[str] = field(default_factory=list)
    known_failure_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hash(scope: str, scope_ref: str | None, proposed: str) -> str:
    norm = " ".join((proposed or "").lower().split())
    return hashlib.sha256(f"{scope}|{scope_ref or ''}|{norm}".encode()).hexdigest()[:32]


class BehaviorStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def propose(
        self, *, trigger: str, proposed_behavior: str, current_behavior: str | None = None,
        reason: str | None = None, scope: str = "user", scope_ref: str | None = None,
        source_type: str = "user_feedback", evidence: dict[str, Any] | None = None,
        confidence: float = 0.5,
    ) -> UUID:
        """Create (or reinforce) a behavior candidate. Deduped by (scope, scope_ref, proposal): a
        repeated signal bumps observation count + confidence rather than duplicating (§13). Never
        auto-accepted — acceptance is a separate, authority-gated step (§42)."""
        h = _hash(scope, scope_ref, proposed_behavior)
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, times_observed FROM behavior_proposal "
                "WHERE scope=$1 AND coalesce(scope_ref,'')=coalesce($2,'') AND content_hash=$3 "
                "  AND status NOT IN ('rejected','superseded') LIMIT 1", scope, scope_ref, h)
            if existing is not None:
                new_conf = min(0.95, 0.5 + 0.1 * existing["times_observed"])
                await conn.execute(
                    "UPDATE behavior_proposal SET times_observed=times_observed+1, "
                    "  confidence=$2, updated_at=now() WHERE id=$1", existing["id"], new_conf)
                await self._emit("behavior.candidate_created", str(existing["id"]),
                                 {"reinforced": True, "scope": scope})
                return UUID(str(existing["id"]))
            bid = uuid4()
            await conn.execute(
                "INSERT INTO behavior_proposal "
                "  (id, scope, scope_ref, trigger, current_behavior, proposed_behavior, reason, "
                "   source_type, evidence, confidence, content_hash) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                bid, scope, scope_ref, trigger, current_behavior, proposed_behavior, reason,
                source_type, evidence or {}, confidence, h)
        await self._emit("behavior.candidate_created", str(bid), {"scope": scope, "trigger": trigger[:120]})
        return bid

    async def observe_feedback(
        self, message: str, *, scope: str = "user", scope_ref: str | None = None,
    ) -> UUID | None:
        """Classify a user message and, if it carries a behavioral signal, record a candidate (§6)."""
        obs = classify_feedback(message)
        if obs is None:
            return None
        return await self.propose(
            trigger=obs.trigger, proposed_behavior=obs.proposed_behavior, scope=scope,
            scope_ref=scope_ref, source_type="user_feedback", reason=f"user feedback ({obs.sentiment})",
            evidence={"sentiment": obs.sentiment, "verbatim": message[:280]}, confidence=0.5)

    async def accept(self, proposal_id: UUID, *, by: str = "user") -> bool:
        """Accept a proposal so it affects future planning (§16). Scope is preserved — accepting a
        project proposal does NOT make it global. Core-behavior changes are gated to the user (§42);
        this method records who accepted it."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE behavior_proposal SET status='accepted', decided_by=$2, decided_at=now(), "
                "  updated_at=now() WHERE id=$1 AND status IN ('candidate','testing') RETURNING id",
                proposal_id, by)
        if row is None:
            return False
        await self._emit("behavior.accepted", str(proposal_id), {"by": by})
        return True

    async def reject(self, proposal_id: UUID, *, by: str = "user", reason: str = "") -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE behavior_proposal SET status='rejected', decided_by=$2, decided_at=now(), "
                "  updated_at=now() WHERE id=$1 AND status NOT IN ('rejected','superseded') RETURNING id",
                proposal_id, by)
        if row is None:
            return False
        await self._emit("behavior.rejected", str(proposal_id), {"by": by, "reason": reason[:200]})
        return True

    async def mark_testing(self, proposal_id: UUID) -> None:
        """Move a candidate into a bounded 'testing' state (§16) — being tried, not yet permanent."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE behavior_proposal SET status='testing', updated_at=now() "
                "WHERE id=$1 AND status='candidate'", proposal_id)
        await self._emit("behavior.testing", str(proposal_id), {})

    async def supersede(self, old_id: UUID, new_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE behavior_proposal SET status='superseded', superseded_by=$2, updated_at=now() "
                "WHERE id=$1", old_id, new_id)
        await self._emit("behavior.superseded", str(old_id), {"new_id": str(new_id)})

    async def pending(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, scope, scope_ref, trigger, proposed_behavior, reason, confidence, "
                "  times_observed, status, created_at FROM behavior_proposal "
                "WHERE status IN ('candidate','testing') ORDER BY confidence DESC, created_at DESC LIMIT $1",
                limit)
        return [dict(r) for r in rows]

    async def accepted(self, *, scope: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clause = "status='accepted'"
        args: list[Any] = []
        if scope is not None:
            args.append(scope)
            clause += f" AND scope=${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, scope, scope_ref, trigger, proposed_behavior, confidence "
                f"FROM behavior_proposal WHERE {clause} ORDER BY confidence DESC LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, count(*) AS n FROM behavior_proposal GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"pending": by.get("candidate", 0) + by.get("testing", 0),
                "accepted": by.get("accepted", 0), "rejected": by.get("rejected", 0),
                "by_status": by}

    async def profile(self) -> BehaviorProfile:
        """DERIVE the behavior profile from durable records (§19): accepted proposals by scope, learned
        tool reliability, and promoted successful/negative lessons. Bounded — a compact snapshot."""
        prof = BehaviorProfile()
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                accepted = await conn.fetch(
                    "SELECT scope, proposed_behavior FROM behavior_proposal WHERE status='accepted' "
                    "ORDER BY confidence DESC LIMIT 40")
                tools = await conn.fetch(
                    "SELECT content FROM memory WHERE claim_key LIKE 'tool:%' AND valid_until IS NULL "
                    "  AND (structured->>'reliability')::float >= 0.7 "
                    "ORDER BY (structured->>'reliability')::float DESC LIMIT 8")
                good = await conn.fetch(
                    "SELECT lesson FROM learning_candidate WHERE verification_state='promoted' "
                    "  AND coalesce(source_type,'') NOT IN ('failure','negative','recurring_failure') "
                    "ORDER BY evidence_level DESC, confidence DESC LIMIT 8")
                bad = await conn.fetch(
                    "SELECT lesson FROM learning_candidate "
                    "WHERE source_type IN ('failure','negative','recurring_failure') "
                    "  AND verification_state IN ('promoted','verified') "
                    "ORDER BY times_failed DESC LIMIT 8")
        for r in accepted:
            beh = r["proposed_behavior"]
            if r["scope"] == "user":
                prof.known_user_preferences.append(beh)
            elif r["scope"] == "project":
                prof.project_preferences.append(beh)
            else:
                prof.workflow_preferences.append(beh)
        prof.tool_preferences = [r["content"] for r in tools]
        prof.successful_patterns = [r["lesson"] for r in good]
        prof.known_failure_patterns = [r["lesson"] for r in bad]
        # communication_style is derived, not fabricated: only what the user actually asked for (§31).
        comm = [p for p in prof.known_user_preferences
                if any(w in p.lower() for w in ("concise", "brief", "detail", "tone", "short", "explain"))]
        prof.communication_style = "; ".join(comm)
        return prof

    async def _emit(self, event_type: str, proposal_id: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, subject_type="behavior", origin="learning",
                data={"proposal_id": proposal_id, **data})
