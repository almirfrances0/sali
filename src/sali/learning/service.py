"""LearningService — consolidate raw activity into knowledge (spec §17-19).

``consolidate`` is the periodic "organise the messy room" pass: it mines successful commands into
evidence-backed procedures, records failures as retrievable episodes, and embeds the new memories
so they're usable. It reads the durable record (tool_execution / agent_runs) rather than hooking
the hot loop, so learning is decoupled and can run on its own schedule.
"""

from __future__ import annotations

from typing import Any

from sali.learning.episodes import consolidate_stm, prune_stm
from sali.learning.failures import record_failures
from sali.learning.model import ConsolidationResult
from sali.learning.procedures import learn_procedures
from sali.memory import embed_worker
from sali.obs.log import get_logger
from sali.provider.base import ModelProvider

log = get_logger("sali.learning")


class LearningService:
    def __init__(self, pool: Any, provider: ModelProvider) -> None:
        self.pool = pool
        self.provider = provider

    async def consolidate(self, *, threshold: int = 2) -> ConsolidationResult:
        """One consolidation pass (§17-19): learn procedures (evidence-gated), record failures +
        their fixes, fold short-term observations into an episode, prune stale raw — then embed."""
        async with self.pool.acquire() as conn, conn.transaction():
            procedures = await learn_procedures(conn, self.provider, threshold=threshold)
            failures = await record_failures(conn)
            episodes = await consolidate_stm(conn, self.provider)
            pruned = await prune_stm(conn)
            for proc in procedures:
                await _emit(conn, "learning.procedure",
                            {"name": proc.name, "evidence": proc.evidence})
            if failures:
                await _emit(conn, "learning.failure", {"count": failures})
            if episodes:
                await _emit(conn, "learning.episode", {"count": episodes})
        await embed_worker.embed_pending(self.pool, self.provider)  # make the new memories usable
        log.info("consolidated", procedures=len(procedures), failures=failures,
                 episodes=episodes, pruned=pruned)
        return ConsolidationResult(procedures=procedures, failures_recorded=failures,
                                   episodes_created=episodes, stm_pruned=pruned)

    async def procedures(self) -> list[dict[str, Any]]:
        """The procedures Sali has learned so far (most-evidenced first)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content, structured, confidence FROM memory "
                "WHERE layer='procedural' AND valid_until IS NULL "
                "ORDER BY (structured->>'evidence')::int DESC NULLS LAST, confidence DESC"
            )
        return [
            {"content": r["content"], "steps": (r["structured"] or {}).get("steps", []),
             "evidence": (r["structured"] or {}).get("evidence"), "confidence": r["confidence"]}
            for r in rows
        ]


async def _emit(conn: Any, event_type: str, payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES ($1,'learning',$2)",
        event_type, payload,
    )
