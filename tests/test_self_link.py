"""Architecture review · Increment 3 — the world-model linker (§1/§10).

The orphan agent:sali node is stitched into one connected self+environment subgraph: it now runs_on the
machine, thinks_with the model, serves the user, and knows where its workspace + source live — stable
graph facts, composed from the twin's real observations, idempotent.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemorySource
from sali.graph.writer import ensure_node
from sali.twin.self_link import link_self

pytestmark = pytest.mark.db

_WS = "/home/almir/Desktop/sali-works"
_SRC = "/home/almir/Desktop/sali"


async def _seed_world(conn: Any) -> Any:
    machine = await ensure_node(conn, node_type="machine", name="kali", canonical_key="machine:kali",
                                source=MemorySource.SYSTEM_OBSERVATION)
    await ensure_node(conn, node_type="person", name="Almir", canonical_key="person:almir",
                      source=MemorySource.USER_EXPLICIT)
    return machine


async def _agent_edges(conn: Any) -> set[tuple[str, str]]:
    rows = await conn.fetch(
        "SELECT e.rel_type, dst.canonical_key FROM graph_edge e "
        "  JOIN graph_node src ON src.id=e.src_id "
        "  JOIN graph_node dst ON dst.id=e.dst_id "
        "WHERE src.canonical_key='agent:sali' AND e.valid_until IS NULL")
    return {(r["rel_type"], r["canonical_key"]) for r in rows}


async def test_link_self_connects_the_agent_subgraph(db_conn: Any) -> None:
    machine = await _seed_world(db_conn)
    await link_self(db_conn, machine_id=machine.id, model_name="sali:latest",
                    workspace=_WS, source_dir=_SRC)

    edges = await _agent_edges(db_conn)
    assert ("runs_on", "machine:kali") in edges
    assert ("thinks_with", "model:sali:latest") in edges
    assert ("serves", "person:almir") in edges
    assert ("works_in", f"path:{_WS}") in edges
    assert ("source_at", f"path:{_SRC}") in edges


async def test_link_self_is_idempotent(db_conn: Any) -> None:
    machine = await _seed_world(db_conn)
    await link_self(db_conn, machine_id=machine.id, model_name="sali:latest", workspace=_WS, source_dir=_SRC)
    first = await _agent_edges(db_conn)
    await link_self(db_conn, machine_id=machine.id, model_name="sali:latest", workspace=_WS, source_dir=_SRC)
    assert await _agent_edges(db_conn) == first  # re-linking re-affirms, never duplicates


async def test_model_swap_supersedes_thinks_with(db_conn: Any) -> None:
    # REGRESSION (BUG 4): `thinks_with` used `relate`, so re-linking after a model swap left TWO current
    # edges and the self-model reported a stale/ambiguous model. set_fact makes it single-valued — the
    # new model supersedes the old, leaving exactly one current thinks_with edge.
    machine = await _seed_world(db_conn)
    await link_self(db_conn, machine_id=machine.id, model_name="sali:latest", workspace=_WS, source_dir=_SRC)
    await link_self(db_conn, machine_id=machine.id, model_name="sali:lite", workspace=_WS, source_dir=_SRC)
    thinks = {(r, k) for (r, k) in await _agent_edges(db_conn) if r == "thinks_with"}
    assert thinks == {("thinks_with", "model:sali:lite")}, thinks  # exactly one current edge, the NEW model


async def test_two_hop_from_agent_reaches_the_environment(db_conn: Any) -> None:
    from sali.graph import traverse

    machine = await _seed_world(db_conn)
    sali_id = await link_self(db_conn, machine_id=machine.id, model_name="sali:latest",
                              workspace=_WS, source_dir=_SRC)
    neighbors = {h["node"].canonical_key for h in await traverse.neighbors(db_conn, sali_id)}
    # from the agent, one hop now reaches the whole self+environment picture (was: nothing)
    assert {"machine:kali", "model:sali:latest", "person:almir", f"path:{_WS}", f"path:{_SRC}"} <= neighbors


async def test_self_model_is_grounded_in_graph_facts_no_drift(live_pool: Any) -> None:
    # The self-model reads the machine/model from the graph, so swapping the model updates it with NO
    # code change — killing the "Qwen vs model:sali:latest" drift the review found.
    from sali.runtime.self_state import SelfStateStore

    async with live_pool.acquire() as conn:
        m = await ensure_node(conn, node_type="machine", name="Kali Rolling",
                              canonical_key="machine:kali", source=MemorySource.SYSTEM_OBSERVATION,
                              props={"os": "Kali GNU/Linux", "arch": "x86_64"})
        await ensure_node(conn, node_type="person", name="Almir", canonical_key="person:almir",
                          source=MemorySource.USER_EXPLICIT)
        await link_self(conn, machine_id=m.id, model_name="qwen3:35b", workspace=_WS, source_dir=_SRC)

    view = await SelfStateStore(live_pool).assemble()
    assert view["environment"]["model"] == "qwen3:35b"          # the ACTUAL model, from the graph
    assert view["environment"]["machine"] == "Kali Rolling"
    assert view["environment"]["workspace"] == _WS and view["environment"]["source"] == _SRC
    assert "qwen3:35b" in view["self_knowledge"]                # composed, not hardcoded
    assert "Qwen model" not in view["self_knowledge"]           # the old drift string is gone
