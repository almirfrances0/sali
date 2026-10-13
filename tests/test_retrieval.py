"""Hybrid retrieval integration: routing dispatches to memory + graph."""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.graph import writer as graph_writer
from sali.memory import writer as mem_writer
from sali.provider.fake import FakeModelProvider
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService

pytestmark = pytest.mark.db


async def test_relational_query_returns_graph_facts(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        vps = await graph_writer.ensure_node(
            c, node_type="host", name="VPS-01", canonical_key="host:vps01",
            source=MemorySource.USER_EXPLICIT,
        )
        docker = await graph_writer.ensure_node(
            c, node_type="service", name="Docker", canonical_key="svc:docker",
            source=MemorySource.USER_EXPLICIT,
        )
        await graph_writer.relate(
            c, src_id=vps.id, dst_id=docker.id, rel_type="runs", source=MemorySource.USER_EXPLICIT
        )

    service = RetrievalService(live_pool, FakeModelProvider())
    plan = classify("what is connected to the VPS-01 host?")
    bundle = await service.gather("what is connected to the VPS-01 host?", plan)

    assert plan.use_graph
    assert any(f.src == "VPS-01" and f.rel == "runs" and f.dst == "Docker" for f in bundle.graph_facts)


async def test_graph_retrieval_walks_two_hops_closest_first(live_pool: Any) -> None:
    # Project X → runs_on → VPS-01 → runs → Docker : a 2-hop chain the 1-hop walk would miss (§16).
    async with live_pool.acquire() as c:
        px = await graph_writer.ensure_node(c, node_type="project", name="Project X",
                                            canonical_key="project:x", source=MemorySource.USER_EXPLICIT)
        vps = await graph_writer.ensure_node(c, node_type="host", name="VPS-9",
                                             canonical_key="host:vps9", source=MemorySource.USER_EXPLICIT)
        docker = await graph_writer.ensure_node(c, node_type="service", name="Docker-9",
                                                canonical_key="svc:docker9", source=MemorySource.USER_EXPLICIT)
        await graph_writer.relate(c, src_id=px.id, dst_id=vps.id, rel_type="runs_on",
                                  source=MemorySource.USER_EXPLICIT)
        await graph_writer.relate(c, src_id=vps.id, dst_id=docker.id, rel_type="runs",
                                  source=MemorySource.USER_EXPLICIT)

    service = RetrievalService(live_pool, FakeModelProvider())
    plan = classify("what is Project X connected to?")
    bundle = await service.gather("what is Project X connected to?", plan)

    facts = {(f.src, f.rel, f.dst): f.hops for f in bundle.graph_facts}
    assert facts.get(("Project X", "runs_on", "VPS-9")) == 1  # direct
    assert facts.get(("VPS-9", "runs", "Docker-9")) == 2      # reached two hops out
    # graph-distance ordering (§38): the 1-hop fact appears before any 2-hop fact
    hops_in_order = [f.hops for f in bundle.graph_facts]
    assert hops_in_order == sorted(hops_in_order)


async def test_plain_lookup_returns_memories_only(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        await mem_writer.remember(
            c, layer=MemoryLayer.SEMANTIC, content="Almir prefers local models",
            source=MemorySource.USER_EXPLICIT,
        )
        await mem_writer.observe(  # noise in the event log; must not pollute a plain lookup
            c, kind="turn", content="hi", source=MemorySource.CONVERSATION
        )

    service = RetrievalService(live_pool, FakeModelProvider())
    plan = classify("what did I say about local models?")
    bundle = await service.gather("what did I say about local models?", plan)

    assert not plan.use_graph and not plan.use_recent
    assert bundle.graph_facts == [] and bundle.recent == []
    assert any("local models" in h.memory.content for h in bundle.memories)


async def test_gather_reinforces_recalled_memories(live_pool: Any) -> None:
    # A memory Sali actually recalls is marked USED (access_count++, last_accessed set). Without this
    # every memory sits at access_count=0 and recall leaves no trace — the core "not using its
    # memory" symptom. Reinforcement must not touch last_verified (recall isn't re-verification).
    async with live_pool.acquire() as c:
        mem = await mem_writer.remember(
            c, layer=MemoryLayer.SEMANTIC, content="Almir prefers local models",
            source=MemorySource.USER_EXPLICIT,
        )
        before = await c.fetchrow("SELECT last_verified FROM memory WHERE id=$1", mem.id)

    service = RetrievalService(live_pool, FakeModelProvider())
    plan = classify("what did I say about local models?")
    bundle = await service.gather("what did I say about local models?", plan)
    assert any(h.memory.id == mem.id for h in bundle.memories)  # it was recalled

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT access_count, last_accessed, last_verified FROM memory WHERE id=$1", mem.id)
    assert row["access_count"] == 1 and row["last_accessed"] is not None
    assert row["last_verified"] == before["last_verified"]  # recall did NOT re-verify


async def test_link_creates_a_retrievable_relationship(live_pool: Any) -> None:
    # The conversational→graph write path: a relationship stated in chat becomes an edge Sali can
    # traverse later — so a relational question answers from STRUCTURE, not text similarity.
    from sali.graph.service import GraphService

    await GraphService(live_pool).link(
        subject="Salix Studio", relation="deployed on", obj="VPS-99",
        source=MemorySource.CONVERSATION,
    )
    service = RetrievalService(live_pool, FakeModelProvider())
    plan = classify("what is Salix Studio connected to?")
    bundle = await service.gather("what is Salix Studio connected to?", plan)
    assert any(f.src == "Salix Studio" and f.rel == "deployed_on" and f.dst == "VPS-99"
               for f in bundle.graph_facts)
