"""GraphService — the faculty facade over pooled connections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sali.core.enums import MemorySource
from sali.graph import traverse as _traverse
from sali.graph import writer as _writer
from sali.graph.models import (
    ContradictionOutcome,
    Edge,
    Node,
    Resolution,
    row_to_edge,
    row_to_node,
)

# Canonical-entity-type priority for tie-breaking name resolution: a first-class entity Sali reasons
# about (its own agent node, the person, the machine, its model) outranks incidental nodes that merely
# contain the same token (a project folder, a filesystem path, an old model variant). Lower wins.
_TYPE_PRIORITY = (
    "CASE node_type "
    "WHEN 'agent' THEN 0 WHEN 'person' THEN 1 WHEN 'machine' THEN 2 WHEN 'model' THEN 3 "
    "WHEN 'service' THEN 4 WHEN 'container' THEN 5 WHEN 'network' THEN 6 WHEN 'project' THEN 7 "
    "WHEN 'software' THEN 8 WHEN 'ext_tool' THEN 9 WHEN 'capability' THEN 10 WHEN 'location' THEN 11 "
    "WHEN 'os_package' THEN 12 WHEN 'environment' THEN 13 WHEN 'hardware' THEN 14 ELSE 15 END")


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

    async def _ranked_rows(self, conn: Any, name: str, limit: int) -> list[Any]:
        """Ranked candidate rows for a surface form. DETERMINISTIC: match precision, then
        canonical-entity-type priority (an 'agent'/'person'/'machine' beats a 'project'/'path' when the
        name ties — so bare 'Sali' resolves to the agent, not the repo or a model variant), then a
        stable id tiebreak. This is the fix for the reported nondeterministic resolution."""
        raw = name.strip()
        if not raw:
            return []
        lower = raw.lower()
        pattern = "%" + raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return list(await conn.fetch(
            "SELECT *, CASE "
            "  WHEN lower(name) = $2 THEN 0 "
            "  WHEN props->'aliases' ? $2 THEN 1 "
            "  WHEN name ILIKE $1 THEN 2 "
            "  ELSE 3 END AS match_rank, "
            f"  {_TYPE_PRIORITY} AS type_priority "
            "FROM graph_node WHERE valid_until IS NULL "
            "AND (name ILIKE $1 OR canonical_key ILIKE $1 OR props->'aliases' ? $2) "
            "ORDER BY match_rank, type_priority, confidence DESC, last_seen DESC, id LIMIT $3",
            pattern, lower, limit))

    async def find_by_name(self, name: str, *, limit: int = 5) -> list[Node]:
        """Map a surface form ('my VPS', 'project-x', 'me', 'Ollama') to CURRENT nodes, best-first and
        deterministically (see ``_ranked_rows``)."""
        async with self.pool.acquire() as conn:
            rows = await self._ranked_rows(conn, name, limit)
        return [row_to_node(r) for r in rows]

    async def resolve(self, name: str, *, limit: int = 8) -> Resolution:
        """Resolve a surface form AND explain it (§5/§6): the chosen node, how it matched, and the other
        entities the same name matched (ambiguity). Used so a graph query is traceable, never a mystery."""
        async with self.pool.acquire() as conn:
            rows = await self._ranked_rows(conn, name, limit)
        if not rows:
            return Resolution(query=name, resolved=None, matched_by="none",
                              candidate_count=0, candidates=[], ambiguous=False)
        top = rows[0]
        matched_by = {0: "exact_name", 1: "alias", 2: "name_substring", 3: "key_substring"}.get(
            int(top["match_rank"]), "unknown")
        same_precision = sum(1 for r in rows if r["match_rank"] == top["match_rank"])
        candidates = [{"name": r["name"], "canonical_key": r["canonical_key"],
                       "node_type": r["node_type"], "match_rank": int(r["match_rank"])} for r in rows]
        return Resolution(query=name, resolved=row_to_node(top), matched_by=matched_by,
                          candidate_count=len(rows), candidates=candidates,
                          ambiguous=same_precision > 1)

    async def get_node(
        self, node_type: str, canonical_key: str, *, as_of: datetime | None = None
    ) -> Node | None:
        async with self.pool.acquire() as conn:
            return await _traverse.get_node(conn, node_type, canonical_key, as_of=as_of)

    async def get(self, node_id: UUID) -> Node | None:
        """Fetch one current node by id — the brain viz clicks a node by UUID (get_node needs a key)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM graph_node WHERE id=$1 AND valid_until IS NULL", node_id)
        return row_to_node(row) if row is not None else None

    async def snapshot(
        self, *, limit: int = 250, exclude_types: Sequence[str] = ("ext_tool",)
    ) -> dict[str, Any]:
        """The brain's initial load: the meaningful, CONNECTED entity core — never the whole hairball.

        Ranked by DEGREE first (hubs — the agent, machine, models, projects — come first, so the returned
        edges actually connect), then confidence/recency. Leaf-explosion types (the ~2400 ext_tool nodes
        hanging off one 'has_tool' hub) are excluded by default and reported in ``collapsed`` so the UI can
        draw them as a single cluster the user expands on demand (LOD, §10)."""
        limit = max(1, min(limit, 2000))
        exclude = list(exclude_types)
        async with self.pool.acquire() as conn:
            nodes = await conn.fetch(
                "WITH deg AS ("
                "  SELECT id, count(*) AS d FROM ("
                "    SELECT src_id AS id FROM graph_edge WHERE valid_until IS NULL AND superseded_by IS NULL"
                "    UNION ALL "
                "    SELECT dst_id FROM graph_edge WHERE valid_until IS NULL AND superseded_by IS NULL"
                "  ) t GROUP BY id) "
                "SELECT n.*, COALESCE(deg.d, 0) AS degree FROM graph_node n "
                "  LEFT JOIN deg ON deg.id = n.id "
                "WHERE n.valid_until IS NULL AND n.node_type <> ALL($2::text[]) "
                "ORDER BY degree DESC, n.confidence DESC, n.last_seen DESC, n.id LIMIT $1",
                limit, exclude)
            ids = [n["id"] for n in nodes]
            edges = await conn.fetch(
                "SELECT * FROM graph_edge WHERE valid_until IS NULL AND superseded_by IS NULL "
                "AND src_id = ANY($1) AND dst_id = ANY($1)", ids)
            collapsed = await conn.fetch(
                "SELECT node_type, count(*) AS n FROM graph_node "
                "WHERE valid_until IS NULL AND node_type = ANY($1::text[]) GROUP BY node_type", exclude)
        return {"nodes": [row_to_node(n) for n in nodes],
                "edges": [row_to_edge(e) for e in edges],
                "collapsed": {r["node_type"]: int(r["n"]) for r in collapsed}}

    async def subgraph(self, node_id: UUID, *, depth: int = 1, limit: int = 80) -> dict[str, list[Any]]:
        """The BIDIRECTIONAL neighborhood around a node (full nodes AND edges, both directions) out to
        `depth` hops — for click-to-expand. neighbors()/traverse() are outbound-only and edge-less, so
        this is the shape the graph view actually needs to draw a subgraph."""
        depth = max(1, min(depth, 3))
        limit = max(1, min(limit, 500))
        async with self.pool.acquire() as conn:
            reached = await conn.fetch(
                "WITH RECURSIVE reach(id, d) AS ("
                "  SELECT $1::uuid, 0 "
                "  UNION "
                "  SELECT CASE WHEN e.src_id = r.id THEN e.dst_id ELSE e.src_id END, r.d + 1 "
                "  FROM reach r JOIN graph_edge e "
                "    ON (e.src_id = r.id OR e.dst_id = r.id) "
                "   AND e.valid_until IS NULL AND e.superseded_by IS NULL "
                "  WHERE r.d < $2"
                ") SELECT DISTINCT id FROM reach LIMIT $3",
                node_id, depth, limit)
            ids = [r["id"] for r in reached]
            nodes = await conn.fetch(
                "SELECT * FROM graph_node WHERE id = ANY($1) AND valid_until IS NULL", ids)
            edges = await conn.fetch(
                "SELECT * FROM graph_edge WHERE valid_until IS NULL AND superseded_by IS NULL "
                "AND src_id = ANY($1) AND dst_id = ANY($1)", ids)
        return {"nodes": [row_to_node(n) for n in nodes],
                "edges": [row_to_edge(e) for e in edges]}

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
        return [row_to_edge(r) for r in rows]


__all__ = ["GraphService", "MemorySource"]
