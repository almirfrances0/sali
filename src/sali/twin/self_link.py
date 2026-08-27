"""Stitch the agent into its world graph (spec §1/§10).

Before this, `agent:sali` was an orphan — its only edge was the reverse `person:almir --uses-->
agent:sali`, so traversing from the agent reached nothing about the machine it runs on, the model it
thinks with, or where its workspace and source live; those facts lived only in a config file and a
hand-written prose string that could drift. This wires the two graph islands (identity + machine) into
ONE connected self+environment subgraph, so "where is my source / what's my model / what machine am I
on" are STABLE graph facts Sali doesn't rediscover — composed from the twin's real observations, not
asserted. Idempotent: ensure_node/relate dedup, so every refresh just re-affirms the links.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.enums import MemorySource
from sali.graph.writer import ensure_node, refresh_props, relate

# Canonical aliases for the agent (§5) — the surface forms that mean Sali-the-agent (NOT the repo,
# a model variant, or a path). So "my AI"/"you" resolve to agent:sali, and the reported bare-"Sali"
# ambiguity is disambiguated to the agent by these + the node-type priority.
_AGENT_ALIASES = ["sali", "sali ai", "sali agent", "the ai", "my ai", "the assistant", "you"]


async def link_self(
    conn: Any, *, machine_id: UUID, model_name: str, workspace: str, source_dir: str,
    source: MemorySource = MemorySource.SYSTEM_OBSERVATION,
) -> UUID:
    """Connect agent:sali to the machine it runs on, the model it thinks with, the user it serves, and
    its workspace + source locations. Reuses the machine node the twin already produced. Returns the
    agent node id. Caller owns the transaction."""
    sali = await ensure_node(
        conn, node_type="agent", name="Sali", canonical_key="agent:sali", source=source)
    await refresh_props(conn, sali.id, {"aliases": _AGENT_ALIASES})  # canonical aliases (§5)

    await relate(conn, src_id=sali.id, dst_id=machine_id, rel_type="runs_on", source=source)

    model = await ensure_node(
        conn, node_type="model", name=model_name, canonical_key=f"model:{model_name}", source=source)
    await relate(conn, src_id=sali.id, dst_id=model.id, rel_type="thinks_with", source=source)

    person = await conn.fetchrow(
        "SELECT id FROM graph_node WHERE canonical_key='person:almir' AND valid_until IS NULL")
    if person is not None:
        await relate(conn, src_id=sali.id, dst_id=person["id"], rel_type="serves", source=source)

    workspace_node = await ensure_node(
        conn, node_type="location", name="workspace", canonical_key=f"path:{workspace}",
        source=source, props={"path": workspace, "role": "workspace"})
    await relate(conn, src_id=sali.id, dst_id=workspace_node.id, rel_type="works_in", source=source)

    source_node = await ensure_node(
        conn, node_type="location", name="source", canonical_key=f"path:{source_dir}",
        source=source, props={"path": source_dir, "role": "source"})
    await relate(conn, src_id=sali.id, dst_id=source_node.id, rel_type="source_at", source=source)

    return sali.id
