"""Memory correctness · Increment 1 — deterministic, explainable entity resolution (§1/§5/§6).

Reproduces the reported bug: a bare name ('Sali') matched many nodes and the tiebreak was
nondeterministic, so two queries disagreed. Now resolution is deterministic (match precision →
canonical-entity-type → id) and returns metadata explaining exactly which entity was chosen and which
others matched.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemorySource
from sali.graph.service import GraphService
from sali.graph.writer import ensure_node, refresh_props

pytestmark = pytest.mark.db


async def _seed_ambiguous_sali(conn: Any) -> None:
    # the exact shape that caused the bug: many nodes contain "sali"
    await ensure_node(conn, node_type="agent", name="Sali", canonical_key="agent:sali",
                      source=MemorySource.USER_EXPLICIT)
    await ensure_node(conn, node_type="project", name="sali", canonical_key="project:sali",
                      source=MemorySource.SYSTEM_OBSERVATION)
    await ensure_node(conn, node_type="project", name="Salix Studio", canonical_key="project:salix",
                      source=MemorySource.CONVERSATION)
    await ensure_node(conn, node_type="model", name="sali:latest", canonical_key="model:sali:latest",
                      source=MemorySource.SYSTEM_OBSERVATION)
    await ensure_node(conn, node_type="location", name="source", canonical_key="path:/home/almir/sali",
                      source=MemorySource.SYSTEM_OBSERVATION)


async def test_bare_sali_resolves_deterministically_to_the_agent(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await _seed_ambiguous_sali(conn)
    graph = GraphService(live_pool)

    # the agent wins over the project/model/path via the node-type priority — every time
    for _ in range(5):
        res = await graph.resolve("Sali")
        assert res.resolved is not None
        assert res.resolved.canonical_key == "agent:sali"
        assert res.matched_by == "exact_name"


async def test_resolution_explains_the_ambiguity(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await _seed_ambiguous_sali(conn)
    res = await GraphService(live_pool).resolve("Sali")
    # it flags that the surface form matched more than one entity, and lists the alternatives (§6)
    assert res.ambiguous is True
    keys = {c["canonical_key"] for c in res.candidates}
    assert {"agent:sali", "project:sali", "model:sali:latest"} <= keys
    assert res.candidate_count >= 4


async def test_unresolvable_name_says_so(live_pool: Any) -> None:
    res = await GraphService(live_pool).resolve("nonexistent-entity-xyz")
    assert res.resolved is None and res.matched_by == "none" and res.candidate_count == 0


async def test_alias_resolves_to_the_agent(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        sali = await ensure_node(conn, node_type="agent", name="Sali", canonical_key="agent:sali",
                                 source=MemorySource.USER_EXPLICIT)
        await refresh_props(conn, sali.id, {"aliases": ["my ai", "you"]})
        await ensure_node(conn, node_type="project", name="my ai project", canonical_key="project:myai",
                          source=MemorySource.CONVERSATION)
    res = await GraphService(live_pool).resolve("my ai")
    assert res.resolved is not None and res.resolved.canonical_key == "agent:sali"
    assert res.matched_by == "alias"  # a declared alias beats a substring of another node's name
