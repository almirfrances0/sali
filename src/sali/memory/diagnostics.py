"""Memory health diagnostics (spec §49) — read-only aggregates over the whole memory subsystem, so
Sali (and Almir, via `sali memory status`) can see the shape and health of what it knows: how much is
stored by layer, how connected the graph is, and where the soft spots are (unverified, low-confidence,
superseded, embed backlog, open contradictions, orphan nodes). Pure reads; never mutates anything."""

from __future__ import annotations

from typing import Any

_LOW_CONFIDENCE = 0.4


async def health(pool: Any) -> dict[str, Any]:
    async with pool.acquire() as conn:
        by_layer = {
            r["layer"]: r["n"] for r in await conn.fetch(
                "SELECT layer::text AS layer, count(*) AS n FROM memory "
                "WHERE valid_until IS NULL AND superseded_by IS NULL GROUP BY layer ORDER BY n DESC")
        }
        memory = {
            "current": sum(by_layer.values()),
            "by_layer": by_layer,
            "superseded": await conn.fetchval("SELECT count(*) FROM memory WHERE superseded_by IS NOT NULL"),
            "unverified": await conn.fetchval(
                "SELECT count(*) FROM memory WHERE valid_until IS NULL AND needs_grounding"),
            "low_confidence": await conn.fetchval(
                "SELECT count(*) FROM memory WHERE valid_until IS NULL AND superseded_by IS NULL "
                "AND confidence < $1", _LOW_CONFIDENCE),
            "embed_backlog": await conn.fetchval(
                "SELECT count(*) FROM memory WHERE embed_status = 'pending'"),
        }
        current_nodes = await conn.fetchval("SELECT count(*) FROM graph_node WHERE valid_until IS NULL")
        current_edges = await conn.fetchval("SELECT count(*) FROM graph_edge WHERE valid_until IS NULL")
        graph = {
            "nodes": current_nodes,
            "edges": current_edges,
            "historical_edges": await conn.fetchval(
                "SELECT count(*) FROM graph_edge WHERE valid_until IS NOT NULL"),
            "orphan_nodes": await conn.fetchval(
                "SELECT count(*) FROM graph_node n WHERE n.valid_until IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM graph_edge e WHERE e.valid_until IS NULL "
                "AND (e.src_id = n.id OR e.dst_id = n.id))"),
        }
        contradictions = {
            "total": await conn.fetchval("SELECT count(*) FROM contradiction"),
            "open": await conn.fetchval("SELECT count(*) FROM contradiction WHERE status = 'open'"),
        }
        events = await conn.fetchval("SELECT count(*) FROM event")
    return {"memory": memory, "graph": graph, "contradictions": contradictions, "events": events}
