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

    async def get_node(
        self, node_type: str, canonical_key: str, *, as_of: datetime | None = None
    ) -> Node | None:
        async with self.pool.acquire() as conn:
            return await _traverse.get_node(conn, node_type, canonical_key, as_of=as_of)

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
