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

    async def gather(
        self, query: str, plan: RetrievalPlan, *, k: int = 6, scope: str | None = None
    ) -> RetrievalBundle:
        memories = (
            await self.memory.retrieve(query, k=k, scope=scope)
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
        seen: set[tuple[str, str, str]] = set()

        def _add(src: str, rel: str, dst: str, confidence: float, hops: int) -> None:
            key = (src, rel, dst)
            if key in seen or src == dst:
                return
            seen.add(key)
            facts.append(GraphFact(src=src, rel=rel, dst=dst, confidence=confidence, hops=hops))

        async with self.pool.acquire() as conn:
            seeds = await conn.fetch(
                "SELECT id, name FROM graph_node WHERE valid_until IS NULL AND name ILIKE ANY($1) "
                "LIMIT 3",
                patterns,
            )
            # Walk TWO hops (§16): the direct relationships of the mentioned entity, then one step
            # further out — so "why is Project X failing?" reaches Project X → Docker → the container,
            # not just Project X's immediate neighbours. Frontier is bounded so it can't fan out.
            frontier: list[tuple[Any, str]] = []
            for seed in seeds:
                for hop in await traverse.neighbors(conn, seed["id"]):
                    _add(seed["name"], hop["rel_type"], hop["node"].name, hop["confidence"], 1)
                    frontier.append((hop["node"].id, hop["node"].name))
            for node_id, node_name in frontier[:8]:
                for hop in await traverse.neighbors(conn, node_id):
                    _add(node_name, hop["rel_type"], hop["node"].name, hop["confidence"], 2)

        # Graph-distance ranking (§38): closest first, then most-confident — an important-but-far fact
        # never crowds out a directly-relevant one.
        facts.sort(key=lambda f: (f.hops, -f.confidence))
        return facts[:k]

    async def _recent(self, k: int) -> list[RecentItem]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_type, created_at FROM event "
                "WHERE event_type NOT IN ('memory.observed') "
                "ORDER BY seq DESC LIMIT $1",
                k,
            )
        return [RecentItem(event_type=r["event_type"], at=r["created_at"]) for r in rows]
