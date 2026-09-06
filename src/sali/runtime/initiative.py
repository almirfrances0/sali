"""InitiativeEngine (§9/§10/§11/§54) — the orchestration layer of Sali's autonomous life.

It answers "given everything Sali currently knows, is there something worthwhile he should do?" by
scanning DURABLE state (overdue commitments, due obligations, active goals needing a next action, stale
digital objects) and turning findings into deduplicated, deterministically-scored initiative candidates.
It does NOT execute anything and it is NOT authority to act (§12/§53) — it decides what deserves
attention; the authority model and the reviewer gate the rest. Scoring is deterministic and observable
(reason codes, not chain-of-thought). It is bounded by an initiative budget and computes the next wake
time so Sali can sleep between cycles instead of burning the GPU (§37/§38/§39/§40).

This is the missing ORCHESTRATION layer (§76) — it owns no data; every source of truth stays where it
already lives (commitments, obligations, goals, external objects, routines).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# deterministic base importance per initiative source (§10) — never the model's say-so
_SOURCE_IMPORTANCE: dict[str, float] = {
    "commitment": 0.80, "obligation": 0.70, "goal": 0.60, "capability_gap": 0.55,
    "open_loop": 0.50, "environment": 0.40, "routine": 0.45, "curiosity": 0.35,
    "self": 0.35,
}


@dataclass(slots=True)
class InitiativeBudget:
    """Restraint for autonomy (§39): bounds per cycle so background cognition never overwhelms the box."""
    max_candidates_per_cycle: int = 8
    max_backoff_attempts: int = 5


def score(*, source: str, urgency: float, risk: float, confidence: float = 0.6) -> dict[str, float]:
    """Deterministic priority scoring (§10). importance (by source) leads; urgency raises it; risk and
    low confidence lower it. Returns the structured scores stored on the initiative — no invented number."""
    importance = _SOURCE_IMPORTANCE.get(source, 0.4)
    priority = 0.5 * importance + 0.3 * urgency + 0.2 * (1.0 - risk) * confidence
    return {"priority_score": round(max(0.0, min(1.0, priority)), 3),
            "urgency_score": round(urgency, 3), "risk_score": round(risk, 3),
            "confidence": round(confidence, 3)}


class InitiativeEngine:
    def __init__(self, pool: Any, publisher: Any = None, *, budget: InitiativeBudget | None = None) -> None:
        self._pool = pool
        self._publisher = publisher
        self._budget = budget or InitiativeBudget()

    async def generate_candidates(self, *, now: datetime | None = None) -> list[str]:
        """Scan durable state for things worth Sali's attention and upsert (dedup) initiative candidates.
        Bounded by the budget. Returns the initiative ids touched. Pure observation → candidates; it
        never acts (§12)."""
        from sali.tasks.commitments import ObligationStore
        from sali.tasks.goals import GoalStore
        from sali.tasks.initiatives import InitiativeStore

        now = now or datetime.now(UTC)
        inits = InitiativeStore(self._pool, self._publisher)
        touched: list[str] = []
        budget = self._budget.max_candidates_per_cycle

        # 1) overdue commitments — highest importance, urgent (§7/§11)
        async with self._pool.acquire() as conn:
            overdue = await conn.fetch(
                "SELECT id, description FROM commitment WHERE status IN ('open','in_progress','blocked') "
                "  AND deadline IS NOT NULL AND deadline < $1 ORDER BY deadline LIMIT $2", now, budget)
        for c in overdue:
            if len(touched) >= budget:
                break
            sc = score(source="commitment", urgency=1.0, risk=0.2)
            iid, _ = await inits.upsert_candidate(
                source="commitment", subject_ref=str(c["id"]),
                title=f"Overdue commitment: {c['description'][:80]}", commitment_id=c["id"],
                reason_codes=["commitment_overdue"], priority_score=sc["priority_score"],
                urgency_score=sc["urgency_score"], risk_score=sc["risk_score"], confidence=sc["confidence"])
            await self._emit("commitment.overdue", {"commitment_id": str(c["id"])})
            touched.append(str(iid))

        # 2) due obligations — the unfinished business that needs attention now (§9/§61)
        for ob in await ObligationStore(self._pool).open(due_only=True, limit=budget):
            if len(touched) >= budget:
                break
            sc = score(source="obligation", urgency=0.7, risk=0.3)
            iid, _ = await inits.upsert_candidate(
                source="obligation", subject_ref=str(ob["id"]),
                title=f"Open obligation: {ob['description'][:80]}", reason_codes=["obligation_due"],
                evidence={"next_action": ob.get("next_action")}, priority_score=sc["priority_score"],
                urgency_score=sc["urgency_score"], risk_score=sc["risk_score"], confidence=sc["confidence"])
            touched.append(str(iid))

        # 3) active goals that declare a next action but aren't progressing right now (§6)
        for g in await GoalStore(self._pool).active(limit=budget):
            if len(touched) >= budget:
                break
            if not g.get("next_action"):
                continue
            urgency = 0.8 if (g.get("deadline") and g["deadline"] < now) else 0.4
            sc = score(source="goal", urgency=urgency, risk=0.25,
                       confidence=min(0.9, 0.5 + 0.05 * (10 - g["priority"])))
            iid, _ = await inits.upsert_candidate(
                source="goal", subject_ref=str(g["id"]), title=f"Advance goal: {g['objective'][:80]}",
                goal_id=g["id"], reason_codes=["goal_next_action"], priority_score=sc["priority_score"],
                urgency_score=sc["urgency_score"], risk_score=sc["risk_score"], confidence=sc["confidence"])
            touched.append(str(iid))

        # 4) stale ACTIVE digital objects — external state should be re-observed before relying on it (§22)
        async with self._pool.acquire() as conn:
            stale = await conn.fetch(
                "SELECT id, service FROM external_entity WHERE status='active' "
                "  AND (last_verified IS NULL OR last_verified < now() - interval '1 day') "
                "ORDER BY updated_at LIMIT $1", budget)
        for o in stale:
            if len(touched) >= budget:
                break
            sc = score(source="environment", urgency=0.3, risk=0.1)
            iid, _ = await inits.upsert_candidate(
                source="environment", subject_ref=str(o["id"]),
                title=f"Re-observe {o['service']} (state may be stale)", reason_codes=["stale_external_state"],
                priority_score=sc["priority_score"], urgency_score=sc["urgency_score"],
                risk_score=sc["risk_score"], confidence=sc["confidence"])
            touched.append(str(iid))

        # 5) open loops (§6): unresolved matters Sali has mental space for. High-priority loops
        # become initiative candidates so they surface for consideration. Low-priority loops stay
        # dormant until they're reinforced (touched again).
        with contextlib.suppress(Exception):
            from sali.tasks.open_loops import OpenLoopStore
            for loop in await OpenLoopStore(self._pool).as_initiative_candidates(limit=budget):
                if len(touched) >= budget:
                    break
                # Urgency scales with the loop's own priority (which rises on reinforcement).
                urgency = min(0.9, 0.3 + 0.7 * float(loop["priority"]))
                sc = score(source="open_loop", urgency=urgency, risk=0.15,
                           confidence=min(0.85, 0.4 + 0.5 * float(loop["priority"])))
                iid, _ = await inits.upsert_candidate(
                    source="open_loop", subject_ref=str(loop["id"]),
                    title=f"Follow up: {str(loop['title'])[:80]}",
                    reason_codes=[f"open_loop_{loop['kind']}"],
                    priority_score=sc["priority_score"], urgency_score=sc["urgency_score"],
                    risk_score=sc["risk_score"], confidence=sc["confidence"])
                touched.append(str(iid))

        # 6) curiosities (§5): knowledge gaps Sali repeatedly encounters. Only the HIGHEST-priority
        # feed the initiative loop — idle-time investigation is legitimate, but background research
        # must not dominate real user work. Deliberately low base importance (0.35) so a strong
        # curiosity never outranks a goal, obligation, or commitment.
        with contextlib.suppress(Exception):
            from sali.learning.curiosity import CuriosityStore
            for c in await CuriosityStore(self._pool).as_initiative_candidates(limit=3):
                if len(touched) >= budget:
                    break
                # Urgency stays low for curiosities — even a strong one is patient work.
                urgency = 0.25 + 0.35 * min(1.0, float(c["times_encountered"]) / 8.0)
                sc = score(source="curiosity", urgency=urgency, risk=0.1,
                           confidence=0.5)
                iid, _ = await inits.upsert_candidate(
                    source="curiosity", subject_ref=str(c["id"]),
                    title=f"Investigate: {str(c['subject'])[:80]}",
                    reason_codes=["knowledge_gap"],
                    priority_score=sc["priority_score"], urgency_score=sc["urgency_score"],
                    risk_score=sc["risk_score"], confidence=sc["confidence"])
                touched.append(str(iid))
        return touched

    async def next_wake(self, *, now: datetime | None = None) -> datetime | None:
        """The earliest time Sali should wake to do something — the min of the next due routine,
        obligation check, commitment deadline, and deferred-initiative retry (§37). None = nothing
        scheduled → Sali can stay asleep until an external event arrives."""
        async with self._pool.acquire() as conn:
            candidates = await conn.fetch(
                "SELECT min(t) AS t FROM ("
                "  SELECT min(next_execution) AS t FROM routine WHERE enabled "
                "  UNION ALL SELECT min(next_check) FROM obligation WHERE status IN ('open','in_progress','blocked') "
                "  UNION ALL SELECT min(deadline) FROM commitment WHERE status IN ('open','in_progress','blocked') "
                "  UNION ALL SELECT min(next_attempt) FROM initiative "
                "    WHERE status NOT IN ('completed','dismissed','expired')"
                ") u")
        val: datetime | None = candidates[0]["t"] if candidates else None
        return val

    async def should_back_off(self, *, source: str, subject_ref: str) -> bool:
        """Whether an initiative has failed too many times and should stop being retried (§40)."""
        from sali.tasks.initiatives import InitiativeStore
        existing = await InitiativeStore(self._pool).by_subject(source=source, subject_ref=subject_ref)
        return bool(existing and existing["attempts"] >= self._budget.max_backoff_attempts)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        import contextlib
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="commitment",
                                       origin="background", data=data)
