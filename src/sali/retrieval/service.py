"""RetrievalService — runs the retrievers a plan selects and returns a fused bundle.

Memory (semantic + lexical, relevance-led) always runs; graph traversal and recent-activity
run only when the router asks for them. Graph seeding is deterministic entity linking:
query tokens are matched against node names, and the neighbours of the matches become facts.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from sali.core.clock import Clock, SystemClock
from sali.graph import traverse
from sali.memory.service import MemoryService
from sali.provider.base import ModelProvider
from sali.retrieval.models import GraphFact, RecentItem, RetrievalBundle
from sali.retrieval.router import RetrievalPlan

_STOP = {
    "the", "are", "and", "for", "which", "what", "does", "did", "how", "why", "who", "your",
    "you", "this", "that", "with", "from", "about", "connected", "related", "projects",
}


class RetrievalService:
    def __init__(self, pool: Any, provider: ModelProvider, clock: Clock | None = None) -> None:
        self.pool = pool
        self.memory = MemoryService(pool, provider, clock)
        self.clock = clock or SystemClock()

    async def gather(self, query: str, plan: RetrievalPlan, *, k: int = 6) -> RetrievalBundle:
        memories = (
            await self.memory.retrieve(query, k=k)
            if (plan.use_vector or plan.use_keyword)
            else []
        )
        # Reinforce what we recalled: a memory Sali actually uses gets marked used (access_count,
        # last_accessed). Best-effort — reinforcement must never break retrieval.
        if memories:
            with contextlib.suppress(Exception):
                await self.memory.touch_many([h.memory.id for h in memories])
        # Graph seeding is always attempted — it is entity-guarded (returns nothing when no
        # node name matches), so any mentioned entity brings in its known relationships
        # regardless of how the question is phrased. The relational intent is a ranking hint.
        graph_facts = await self._graph_facts(query, k)
        recent = await self._recent(k) if plan.use_recent else []
        return RetrievalBundle(memories=memories, graph_facts=graph_facts, recent=recent)

    async def _graph_facts(self, query: str, k: int) -> list[GraphFact]:
        words = [
            w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}", query)
            if w.lower() not in _STOP
        ]
        if not words:
            return []
        # Escape LIKE metacharacters so a token like "sali_demo" matches a literal underscore,
        # not ILIKE's single-char wildcard (which would over-match unrelated node names).
        def _esc(w: str) -> str:
            return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        patterns = [f"%{_esc(w)}%" for w in words[:6]]
        facts: list[GraphFact] = []
        async with self.pool.acquire() as conn:
            seeds = await conn.fetch(
                "SELECT id, name FROM graph_node WHERE valid_until IS NULL AND name ILIKE ANY($1) "
                "LIMIT 3",
                patterns,
            )
            for seed in seeds:
                for hop in await traverse.neighbors(conn, seed["id"]):
                    facts.append(
                        GraphFact(
                            src=seed["name"],
                            rel=hop["rel_type"],
                            dst=hop["node"].name,
                            confidence=hop["confidence"],
                        )
                    )
                    if len(facts) >= k:
                        return facts
        return facts

    async def _recent(self, k: int) -> list[RecentItem]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_type, created_at FROM event "
                "WHERE event_type NOT IN ('memory.observed') "
                "ORDER BY seq DESC LIMIT $1",
                k,
            )
        return [RecentItem(event_type=r["event_type"], at=r["created_at"]) for r in rows]
