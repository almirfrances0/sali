"""Graph traversal — multi-hop recursive CTE with a cycle guard, current or as-of.

The as-of predicate ``valid_from <= t AND (valid_until IS NULL OR valid_until > t)`` is the
same shape memory uses, so entity-time and belief-time travel agree by construction. This is
how Sali answers "which projects are connected to the VPS?" (walk the edges) and "what model
was I using last month?" (walk them as-of a time) structurally, not by text similarity.

Only *edges* are filtered temporally: an edge's ``dst_id`` already pins the exact node
version it pointed to, so the node join needs no temporal predicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sali.graph.models import Node, row_to_node

_MAX_DEPTH = 4


def _edge_pred(alias: str, as_of_param: str | None) -> str:
    if as_of_param is None:
        return f"{alias}.valid_until IS NULL AND {alias}.superseded_by IS NULL"
    return (
        f"{alias}.valid_from <= {as_of_param} "
        f"AND ({alias}.valid_until IS NULL OR {alias}.valid_until > {as_of_param})"
    )


async def get_node(
    conn: Any, node_type: str, canonical_key: str, *, as_of: datetime | None = None
) -> Node | None:
    if as_of is None:
        row = await conn.fetchrow(
            "SELECT * FROM graph_node WHERE node_type=$1 AND canonical_key=$2 AND valid_until IS NULL",
            node_type, canonical_key,
        )
    else:
        row = await conn.fetchrow(
            "SELECT * FROM graph_node WHERE node_type=$1 AND canonical_key=$2 "
            "AND valid_from <= $3 AND (valid_until IS NULL OR valid_until > $3)",
            node_type, canonical_key, as_of,
        )
    return row_to_node(row) if row is not None else None


async def neighbors(
    conn: Any,
    node_id: UUID,
    *,
    rel_types: Sequence[str] | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Direct successors: [{rel_type, confidence, node}] over current or as-of edges."""
    rels = list(rel_types) if rel_types else None
    if as_of is None:
        sql = (
            f"SELECT e.rel_type, e.confidence, n.* FROM graph_edge e "
            f"JOIN graph_node n ON n.id = e.dst_id "
            f"WHERE e.src_id = $1 AND {_edge_pred('e', None)} "
            f"AND ($2::text[] IS NULL OR e.rel_type = ANY($2)) ORDER BY e.rel_type, n.name"
        )
        rows = await conn.fetch(sql, node_id, rels)
    else:
        sql = (
            f"SELECT e.rel_type, e.confidence, n.* FROM graph_edge e "
            f"JOIN graph_node n ON n.id = e.dst_id "
            f"WHERE e.src_id = $1 AND {_edge_pred('e', '$3')} "
            f"AND ($2::text[] IS NULL OR e.rel_type = ANY($2)) ORDER BY e.rel_type, n.name"
        )
        rows = await conn.fetch(sql, node_id, rels, as_of)
    return [
        {"rel_type": r["rel_type"], "confidence": r["confidence"], "node": row_to_node(r)}
        for r in rows
    ]


async def traverse(
    conn: Any,
    start_id: UUID,
    *,
    max_depth: int = _MAX_DEPTH,
    rel_types: Sequence[str] | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Reachable nodes up to ``max_depth`` hops, cycle-guarded (shortest depth per node)."""
    rels = list(rel_types) if rel_types else None
    as_of_param = None if as_of is None else "$4"
    sql = f"""
        WITH RECURSIVE walk AS (
            SELECT e.dst_id AS node_id, ARRAY[e.src_id, e.dst_id]::uuid[] AS path, 1 AS depth
            FROM graph_edge e
            WHERE e.src_id = $1 AND {_edge_pred('e', as_of_param)}
              AND ($2::text[] IS NULL OR e.rel_type = ANY($2))
            UNION ALL
            SELECT e.dst_id, w.path || e.dst_id, w.depth + 1
            FROM walk w JOIN graph_edge e ON e.src_id = w.node_id
            WHERE w.depth < $3 AND {_edge_pred('e', as_of_param)}
              AND NOT e.dst_id = ANY(w.path)
              AND ($2::text[] IS NULL OR e.rel_type = ANY($2))
        )
        SELECT DISTINCT ON (w.node_id) w.depth, w.node_id, n.name, n.node_type
        FROM walk w JOIN graph_node n ON n.id = w.node_id
        ORDER BY w.node_id, w.depth
    """
    params: list[Any] = [start_id, rels, max_depth]
    if as_of is not None:
        params.append(as_of)
    rows = await conn.fetch(sql, *params)
    result = [
        {"depth": r["depth"], "node_id": r["node_id"], "name": r["name"], "node_type": r["node_type"]}
        for r in rows
    ]
    result.sort(key=lambda x: (x["depth"], x["name"]))
    return result
