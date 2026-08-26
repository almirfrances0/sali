"""MemoryService — the faculty facade the rest of Sali uses.

Owns the pool + provider + clock and exposes remember/observe/retrieve/embed. Retrieval
fuses the lexical and semantic retrievers deterministically (fix M14): **relevance leads**
(semantic similarity, with a floor for lexical matches), and trust only *modulates* it
(``effective_confidence = confidence x freshness_factor``) so a high-importance but weakly-
relevant memory can never outrank a clearly-relevant one. Stale hits are labeled, never
asserted (engineering rule 9).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.clock import Clock, SystemClock
from sali.core.enums import FreshnessPolicy, MemoryLayer, MemorySource
from sali.memory import embed_worker, retriever, writer
from sali.memory.decay import effective_confidence, freshness_factor, is_stale
from sali.memory.models import Memory, MemoryHit, row_to_memory
from sali.provider.base import ModelProvider

# Lexical matches carry no cosine score; give them a sensible relevance floor, nudged by rank.
_KEYWORD_FLOOR = 0.55
_BOTH_BONUS = 0.05


class MemoryService:
    def __init__(self, pool: Any, provider: ModelProvider, clock: Clock | None = None) -> None:
        self.pool = pool
        self.provider = provider
        self.clock = clock or SystemClock()

    async def remember(self, **kwargs: Any) -> Memory:
        async with self.pool.acquire() as conn:
            return await writer.remember(conn, **kwargs)

    async def observe(self, **kwargs: Any) -> UUID:
        async with self.pool.acquire() as conn:
            return await writer.observe(conn, **kwargs)

    async def embed_pending(self, batch: int = 32) -> int:
        return await embed_worker.embed_pending(self.pool, self.provider, batch)

    async def retrieve(self, query: str, k: int = 8) -> list[MemoryHit]:
        query_vec = (await self.provider.embed([embed_worker.QUERY_PREFIX + query]))[0]
        async with self.pool.acquire() as conn:
            vector_rows = await retriever.retrieve_vector(conn, query_vec, k)
            keyword_rows = await retriever.retrieve_keyword(conn, query, k)

        fused: dict[UUID, dict[str, Any]] = {}
        for row in vector_rows:
            entry = fused.setdefault(
                row["id"], {"row": row, "similarity": None, "retrievers": set()}
            )
            entry["similarity"] = row["similarity"]
            entry["retrievers"].add("vector")
        for rank, row in enumerate(keyword_rows):
            entry = fused.setdefault(
                row["id"], {"row": row, "similarity": None, "retrievers": set()}
            )
            entry["retrievers"].add("keyword")
            entry["kw_rank"] = rank

        now = self.clock.now()
        hits = [self._to_hit(entry, now) for entry in fused.values()]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _to_hit(self, entry: dict[str, Any], now: Any) -> MemoryHit:
        row = entry["row"]
        retrievers: set[str] = entry["retrievers"]

        # Relevance leads: cosine similarity, or a rank-nudged floor for lexical-only hits.
        relevance = 0.0
        if entry["similarity"] is not None:
            relevance = max(relevance, float(entry["similarity"]))
        if "keyword" in retrievers:
            relevance = max(relevance, _KEYWORD_FLOOR - 0.01 * entry.get("kw_rank", 0))
        if len(retrievers) > 1:
            relevance = min(1.0, relevance + _BOTH_BONUS)

        policy = FreshnessPolicy(row["freshness"])
        ff = freshness_factor(policy, row["last_verified"], now)
        eff_conf = effective_confidence(row["confidence"], ff)

        # Trust only *modulates* relevance (never inverts it).
        score = relevance * (0.7 + 0.3 * eff_conf)
        which = "hybrid" if len(retrievers) > 1 else next(iter(retrievers))
        return MemoryHit(
            memory=row_to_memory(row),
            score=score,
            effective_confidence=eff_conf,
            freshness_factor=ff,
            stale=is_stale(policy, row["last_verified"], now),
            similarity=entry["similarity"],
            retriever=which,
        )

    async def touch(self, memory_id: UUID) -> None:
        """Access reinforcement: bump access_count and reset the decay clock."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE memory SET access_count = access_count + 1, last_accessed = now() "
                "WHERE id = $1",
                memory_id,
            )


__all__ = ["MemoryLayer", "MemoryService", "MemorySource"]
