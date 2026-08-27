"""MemoryService — the faculty facade the rest of Sali uses.

Owns the pool + provider + clock and exposes remember/observe/retrieve/embed. Retrieval
fuses the lexical and semantic retrievers deterministically (fix M14): **relevance leads**
(semantic similarity, with a floor for lexical matches), and trust only *modulates* it
(``effective_confidence = confidence x freshness_factor``) so a high-importance but weakly-
relevant memory can never outrank a clearly-relevant one. Stale hits are labeled, never
asserted (engineering rule 9).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sali.core.clock import Clock, SystemClock
from sali.core.enums import FreshnessPolicy, MemoryLayer, MemorySource
from sali.core.errors import ProviderError
from sali.memory import embed_worker, retriever, writer
from sali.memory.decay import (
    decayed_importance,
    effective_confidence,
    freshness_factor,
    is_stale,
)
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

    async def retrieve(
        self, query: str, k: int = 8, *, scope: str | None = None, layer: str | None = None
    ) -> list[MemoryHit]:
        # The embedder is a SEPARATE (CPU-only) model — it can be down while chat works. If it is,
        # degrade to lexical-only recall rather than failing the whole turn (§53 graceful degradation).
        # `layer` scopes recall to one memory layer with a real SQL predicate (e.g. pull only
        # PROCEDURAL memories for a task turn) — no top-k blind spot.
        try:
            query_vec: list[float] | None = (await self.provider.embed([embed_worker.QUERY_PREFIX + query]))[0]
        except ProviderError:
            query_vec = None
        async with self.pool.acquire() as conn:
            vector_rows = (await retriever.retrieve_vector(conn, query_vec, k, layer=layer)
                           if query_vec is not None else [])
            keyword_rows = await retriever.retrieve_keyword(conn, query, k, layer=layer)

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
        hits = [self._to_hit(entry, now, scope) for entry in fused.values()]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _to_hit(self, entry: dict[str, Any], now: Any, active_scope: str | None = None) -> MemoryHit:
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

        # Rerank by more than similarity (§37): relevance LEADS, then trust + decayed-importance
        # modulate it, and reuse amplifies an already-relevant hit. Importance decay is per-layer
        # (the half-life comes from layer_policy), so identity/preferences barely decay while a
        # transient system fact sinks (§28); reactivation lets an often-recalled memory rise (§29).
        # Relevance still dominates, so an important-but-irrelevant memory stays suppressed (§38).
        half_life = timedelta(seconds=float(row["half_life_s"])) if row["half_life_s"] else timedelta(days=30)
        dimp = decayed_importance(float(row["importance"]), half_life, now - row["last_verified"])
        reuse = min(0.2, 0.04 * int(row["access_count"] or 0))
        # Scope (§30/§31): in the current project, its memories are boosted and unrelated projects'
        # are damped — global memories stay neutral (they apply everywhere). Relevance still leads.
        m_scope = row.get("scope", "global")
        if active_scope and m_scope not in ("global", active_scope):
            scope_factor = 0.6
        elif active_scope and m_scope == active_scope:
            scope_factor = 1.2
        else:
            scope_factor = 1.0
        score = relevance * (0.6 + 0.25 * eff_conf + 0.15 * dimp) * (1.0 + reuse) * scope_factor
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

    async def forget_matching(self, query: str, *, reason: str) -> dict[str, Any]:
        """Retire the memory that best matches `query` (a belief Sali has corrected). Reversible —
        the interval is closed, not deleted. Returns what was forgotten, or found=False."""
        hits = await self.retrieve(query, k=1)
        if not hits:
            return {"found": False}
        top = hits[0].memory
        async with self.pool.acquire() as conn:
            closed = await writer.forget(conn, top.id, reason=reason)
        return {"found": True, "forgot": top.content, "closed": closed}

    async def verify_matching(self, query: str, *, verified: bool, note: str | None = None) -> dict[str, Any]:
        """Re-ground the memory best matching `query` against reality (§40) — confirmed or contradicted."""
        hits = await self.retrieve(query, k=1)
        if not hits:
            return {"found": False}
        top = hits[0].memory
        async with self.pool.acquire() as conn:
            updated = await writer.reground(conn, top.id, verified=verified, note=note)
        return {"found": True, "memory": top.content, "verified": verified, "updated": updated}

    async def touch(self, memory_id: UUID) -> None:
        """Access reinforcement: bump access_count and record it was just used."""
        await self.touch_many([memory_id])

    async def touch_many(self, memory_ids: list[UUID]) -> None:
        """Reinforce a whole recalled batch in one write: bump access_count and last_accessed for
        the memories Sali actually used this turn. This is what makes a fact that keeps coming up
        *count as used* — without it every memory sits at access_count=0 and recall leaves no trace.
        Note: it does NOT touch last_verified — recalling a fact is not re-verifying it's still true."""
        if not memory_ids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE memory SET access_count = access_count + 1, last_accessed = now() "
                "WHERE id = ANY($1::uuid[])",
                memory_ids,
            )


__all__ = ["MemoryLayer", "MemoryService", "MemorySource"]
