"""Active memory recall (§34,§56): the tools that let Sali QUERY its memory — search, graph, history."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemoryLayer, MemorySource
from sali.graph.service import GraphService
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider
from sali.runtime.loop import _RecallSink
from sali.tools.builtins.recall_tool import (
    MemoryEntity,
    MemoryHistory,
    MemoryRelated,
    MemorySearch,
)
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


async def _wire(pool: Any) -> tuple[_RecallSink, MemoryService, GraphService]:
    mem = MemoryService(pool, FakeModelProvider())
    graph = GraphService(pool)
    return _RecallSink(mem, graph), mem, graph


def _ctx(recall: _RecallSink) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), recall=recall)


async def test_search_returns_a_seeded_fact_with_provenance(live_pool: Any) -> None:
    recall, mem, _ = await _wire(live_pool)
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="Project X uses PostgreSQL for storage",
                       source=MemorySource.USER_EXPLICIT, importance=0.8)
    await mem.embed_pending()
    hits = await recall.search("what database does project x use")
    match = next((h for h in hits if "PostgreSQL" in h["content"]), None)
    assert match is not None
    assert match["source"] == "user_explicit"  # provenance carried
    assert 0.0 <= match["confidence"] <= 1.0 and "stale" in match  # confidence + freshness carried


async def test_search_of_the_unknown_returns_nothing_not_a_guess(live_pool: Any) -> None:
    recall, _, _ = await _wire(live_pool)
    hits = await recall.search("the quetzal migration protocol of 1823")
    assert hits == []  # no evidence → empty, never an invented memory (§47)


async def test_related_and_entity_walk_the_graph(live_pool: Any) -> None:
    recall, _, graph = await _wire(live_pool)
    await graph.link(subject="Almir", relation="owns", obj="VPS-01", source=MemorySource.USER_EXPLICIT)
    await graph.link(subject="VPS-01", relation="runs", obj="Docker", source=MemorySource.USER_EXPLICIT)

    related = await recall.related("VPS-01")
    assert related["found"]
    assert any(r["target"] == "Docker" and r["relation"] == "runs" for r in related["relations"])

    snap = await recall.entity("vps-01")  # case-insensitive resolution
    assert snap["found"] and snap["name"] == "VPS-01"
    assert any(r["target"] == "Docker" for r in snap["relations"])


async def test_history_traces_a_functional_fact_over_time(live_pool: Any) -> None:
    recall, _, graph = await _wire(live_pool)
    ollama = await graph.ensure_node(node_type="software", name="Ollama",
                                     canonical_key="software:ollama", source=MemorySource.SYSTEM_OBSERVATION)
    v1 = await graph.ensure_node(node_type="version", name="0.32",
                                 canonical_key="version:0.32", source=MemorySource.SYSTEM_OBSERVATION)
    v2 = await graph.ensure_node(node_type="version", name="0.33",
                                 canonical_key="version:0.33", source=MemorySource.SYSTEM_OBSERVATION)
    await graph.set_fact(src_id=ollama.id, rel_type="has_version", dst_id=v1.id,
                         source=MemorySource.SYSTEM_OBSERVATION)
    await graph.set_fact(src_id=ollama.id, rel_type="has_version", dst_id=v2.id,
                         source=MemorySource.SYSTEM_OBSERVATION)  # supersedes 0.32

    hist = await recall.history("Ollama", "has_version")
    assert hist["found"] and len(hist["timeline"]) == 2
    values = {e["value"]: e["current"] for e in hist["timeline"]}
    assert values["0.32"] is False and values["0.33"] is True  # old superseded, new current


async def test_recall_tools_end_to_end(live_pool: Any) -> None:
    recall, mem, graph = await _wire(live_pool)
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="Almir prefers ruff for linting",
                       source=MemorySource.USER_EXPLICIT, importance=0.9)
    await mem.embed_pending()
    await graph.link(subject="Almir", relation="uses", obj="Ollama", source=MemorySource.USER_EXPLICIT)
    ctx = _ctx(recall)

    search = await MemorySearch().run({"query": "what does Almir prefer for linting"}, ctx)
    assert search.ok and any("ruff" in m["content"] for m in search.output["matches"])

    related = await MemoryRelated().run({"entity": "Almir"}, ctx)
    assert related.ok and any(r["target"] == "Ollama" for r in related.output["relations"])

    entity = await MemoryEntity().run({"name": "Almir"}, ctx)
    assert entity.ok and entity.output["found"]

    hist = await MemoryHistory().run({"entity": "Nonesuch", "relation": "uses"}, ctx)
    assert hist.ok and not hist.output["found"]  # unknown → graceful empty, no invention


async def test_search_surfaces_the_structured_incident(live_pool: Any) -> None:
    recall, mem, _ = await _wire(live_pool)
    await mem.remember(
        layer=MemoryLayer.EPISODIC, content="fixed the docker port conflict last time",
        source=MemorySource.SYSTEM_OBSERVATION,
        structured={"kind": "incident", "error": "port 8080 in use", "correction": "stopped the other service"})
    await mem.embed_pending()
    hits = await recall.search("docker port problem", layer="episodic")
    match = next((h for h in hits if "docker" in h["content"]), None)
    assert match is not None
    assert match["detail"]["kind"] == "incident" and "8080" in match["detail"]["error"]


async def test_recall_tools_guard_missing_handle() -> None:
    res = await MemorySearch().run({"query": "x"}, ToolContext(settings=Settings(), clock=SystemClock()))
    assert not res.ok and "recall" in (res.error or "")
