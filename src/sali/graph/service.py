"""GraphService — the faculty facade over pooled connections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sali.core.enums import MemorySource
from sali.graph import traverse as _traverse
from sali.graph import writer as _writer
from sali.graph.models import ContradictionOutcome, Edge, Node


class GraphService:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def ensure_node(self, **kwargs: Any) -> Node:
        async with self.pool.acquire() as conn:
            return await _writer.ensure_node(conn, **kwargs)

    async def relate(self, **kwargs: Any) -> Edge:
        async with self.pool.acquire() as conn:
            return await _writer.relate(conn, **kwargs)

    async def link(
        self, *, subject: str, relation: str, obj: str, source: MemorySource,
        confidence: float = 0.6,
    ) -> Edge:
        """Assert a relationship stated in conversation: ensure both endpoints exist (generic
        'entity' nodes keyed by lowercased name, so the same thing mentioned twice is one node) and
        relate them — all in ONE transaction. This is the conversational→graph write path: without
        it, relationships Almir states never become edges Sali can traverse later."""
        def _key(name: str) -> str:
            return f"entity:{name.strip().lower()}"

        rel = relation.strip().lower().replace(" ", "_")
        async with self.pool.acquire() as conn, conn.transaction():
            src = await _writer.ensure_node(
                conn, node_type="entity", name=subject.strip(), canonical_key=_key(subject),
                source=source, confidence=confidence,
            )
            dst = await _writer.ensure_node(
                conn, node_type="entity", name=obj.strip(), canonical_key=_key(obj),
                source=source, confidence=confidence,
            )
            return await _writer.relate(
                conn, src_id=src.id, dst_id=dst.id, rel_type=rel, source=source, confidence=confidence,
            )

    async def set_fact(self, **kwargs: Any) -> tuple[Edge, ContradictionOutcome | None]:
        async with self.pool.acquire() as conn:
            return await _writer.set_fact(conn, **kwargs)

    async def neighbors(
        self, node_id: UUID, *, rel_types: Sequence[str] | None = None, as_of: datetime | None = None
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            return await _traverse.neighbors(conn, node_id, rel_types=rel_types, as_of=as_of)

    async def traverse(
        self,
        start_id: UUID,
        *,
        max_depth: int = 4,
        rel_types: Sequence[str] | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            return await _traverse.traverse(
                conn, start_id, max_depth=max_depth, rel_types=rel_types, as_of=as_of
            )

    async def find_by_name(self, name: str, *, limit: int = 5) -> list[Node]:
        """Entity resolution (§18/§19): map a surface form ('my VPS', 'project-x', 'me', 'Ollama') to
        CURRENT nodes. Ranked so an EXACT name or a declared ALIAS beats a mere substring, and a
        substring of the *name* beats one that only appears in the canonical key (a filesystem path
        like /home/almir/… must never out-resolve the person named Almir)."""
        from sali.graph.models import row_to_node

        raw = name.strip()
        if not raw:
            return []
        lower = raw.lower()
        pattern = "%" + raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT *, CASE "
                "  WHEN lower(name) = $2 THEN 0 "
                "  WHEN props->'aliases' ? $2 THEN 1 "
                "  WHEN name ILIKE $1 THEN 2 "
                "  ELSE 3 END AS match_rank "
                "FROM graph_node WHERE valid_until IS NULL "
                "AND (name ILIKE $1 OR canonical_key ILIKE $1 OR props->'aliases' ? $2) "
                "ORDER BY match_rank, confidence DESC, last_seen DESC LIMIT $3",
                pattern, lower, limit,
            )
        return [row_to_node(r) for r in rows]

    async def get_node(
        self, node_type: str, canonical_key: str, *, as_of: datetime | None = None
    ) -> Node | None:
        async with self.pool.acquire() as conn:
            return await _traverse.get_node(conn, node_type, canonical_key, as_of=as_of)

    async def names_for(self, ids: Sequence[UUID]) -> dict[UUID, str]:
        """Resolve node ids to names in one query — for naming the targets of a history timeline."""
        if not ids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name FROM graph_node WHERE id = ANY($1)", list(ids))
        return {r["id"]: r["name"] for r in rows}

    async def history(self, src_id: UUID, rel_type: str) -> list[Edge]:
        """The full timeline of a functional slot — current and superseded, oldest first."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM graph_edge WHERE src_id=$1 AND rel_type=$2 ORDER BY valid_from",
                src_id, rel_type,
            )
        from sali.graph.models import row_to_edge

        return [row_to_edge(r) for r in rows]


__all__ = ["GraphService", "MemorySource"]
