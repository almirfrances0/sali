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
