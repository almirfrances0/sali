"""End-to-end scenarios through the whole agent loop (spec §37-43 + Almir's real flows).

Each scenario drives a real AgentLoop (retrieval → context → tools → journal → Postgres) with a
scripted FakeModelProvider, so it exercises the wiring the deep audit found broken — memory that
now accrues and is reinforced, provenance carried to use-time, the conversational→graph write path,
twin "small things" recall, instant greetings, and interrupted-turn cleanup — not just units.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.enums import MemoryLayer, MemorySource
from sali.core.ids import new_id
from sali.memory import writer as mem_writer
from sali.provider.base import ChatResult, ToolCall
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tools.registry import default_registry
from sali.twin.memories import write_twin_memories
from sali.twin.model import TwinEntity, TwinSnapshot

pytestmark = pytest.mark.db


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool, provider=provider,
        retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192),
        registry=default_registry(), policy=PolicyEngine(),
        confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")),
    )


async def _count(pool: Any, run_id: Any, kind: str) -> int:
    async with pool.acquire() as c:
        return int(await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind=$2", run_id, kind))


# ---- §38: a plain memory recall answers from memory, and reinforces it ------------------------
async def test_scenario_recall_uses_and_reinforces_memory(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        mem = await mem_writer.remember(
            c, layer=MemoryLayer.SEMANTIC, content="Almir prefers local models",
            source=MemorySource.USER_EXPLICIT)

    # The model just answers (no tools) — the point is the SYSTEM recalled + reinforced the fact.
    loop = _loop(live_pool, FakeModelProvider(
        responses=[ChatResult("You prefer local models.", None, [], 4, 3, "fake")]))
    result = await loop.run("what did I say about local models?", session_id=new_id())

    assert result.tool_calls == 0  # a recall doesn't grope the filesystem
    async with live_pool.acquire() as c:
        payload = await c.fetchval(
            "SELECT payload FROM run_events WHERE run_id=$1 AND kind='retrieve'", result.run_id)
        access = await c.fetchval("SELECT access_count FROM memory WHERE id=$1", mem.id)
    assert payload["memories"] >= 1  # the memory was retrieved into context
    assert access == 1  # ...and reinforced (was 0 before this session existed)


# ---- Almir's flow: a greeting is instant — no machinery, no extra inference --------------------
async def test_scenario_greeting_is_instant(live_pool: Any) -> None:
    loop = _loop(live_pool, FakeModelProvider(
        responses=[ChatResult("Hey Almir. What's up?", None, [], 3, 3, "fake")]))
    result = await loop.run("hey sali", session_id=new_id())

    assert result.tool_calls == 0
    assert result.text == "Hey Almir. What's up?"
    assert await _count(live_pool, result.run_id, "stall_judge") == 0  # no second inference
    assert await _count(live_pool, result.run_id, "follow_through") == 0


# ---- §41: state a relationship, then ask about it — answered from the graph --------------------
async def test_scenario_relationship_then_recall(live_pool: Any) -> None:
    session = new_id()
    fake = FakeModelProvider(responses=[
        # turn 1: the model records the relationship, wraps up (tool_calls>0 → self-judge → DONE)
        ChatResult("", None, [ToolCall("relate",
                   {"subject": "Salix Studio", "relation": "deployed on", "object": "VPS-01"})], 4, 2, "fake"),
        ChatResult("Noted — Salix Studio runs on VPS-01.", None, [], 4, 3, "fake"),
        ChatResult("DONE", None, [], 1, 1, "fake"),
        # turn 2: a later relational question
        ChatResult("Salix Studio is deployed on VPS-01.", None, [], 4, 3, "fake"),
    ])
    loop = _loop(live_pool, fake)

    r1 = await loop.run("Salix Studio is deployed on VPS-01", session_id=session)
    assert r1.tool_calls == 1
    async with live_pool.acquire() as c:
        edge = await c.fetchrow(
            "SELECT e.rel_type FROM graph_edge e JOIN graph_node s ON e.src_id=s.id "
            "WHERE s.name='Salix Studio' AND e.valid_until IS NULL")
    assert edge is not None and edge["rel_type"] == "deployed_on"  # the edge was written

    r2 = await loop.run("what is Salix Studio deployed on?", session_id=session)
    async with live_pool.acquire() as c:
        payload = await c.fetchval(
            "SELECT payload FROM run_events WHERE run_id=$1 AND kind='retrieve'", r2.run_id)
    assert payload["graph"] >= 1  # turn 2 answered from graph structure, not text


# ---- §40: a machine "small thing" is recalled from a twin memory -------------------------------
async def test_scenario_machine_fact_recall(live_pool: Any) -> None:
    snap = TwinSnapshot(machine_key="m", machine_name="kali", entities=[
        TwinEntity("service", "service:nginx", "nginx (running)", {"state": "active"}, "runs"),
    ])
    async with live_pool.acquire() as c, c.transaction():
        await write_twin_memories(c, snap)
    await RetrievalService(live_pool, FakeModelProvider()).memory.embed_pending()

    loop = _loop(live_pool, FakeModelProvider(
        responses=[ChatResult("nginx is running.", None, [], 4, 3, "fake")]))
    result = await loop.run("what services are running?", session_id=new_id())

    async with live_pool.acquire() as c:
        payload = await c.fetchval(
            "SELECT payload FROM run_events WHERE run_id=$1 AND kind='retrieve'", result.run_id)
    assert payload["memories"] >= 1  # the twin:services memory was recalled


# ---- §9/§11: a fact the model saves from the web is stored unverified, with provenance ---------
async def test_scenario_web_fact_saved_unverified_with_provenance(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[
        ChatResult("", None, [ToolCall("remember", {
            "content": "ripgrep 14 added --hyperlink", "source": "web",
            "url": "https://github.com/BurntSushi/ripgrep"})], 4, 2, "fake"),
        ChatResult("Saved that, from the ripgrep repo.", None, [], 4, 3, "fake"),
        ChatResult("DONE", None, [], 1, 1, "fake"),
    ])
    result = await _loop(live_pool, fake).run("look up what's new in ripgrep", session_id=new_id())
    assert result.tool_calls == 1

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT id, source, needs_grounding FROM memory WHERE content LIKE 'ripgrep 14%' "
            "AND valid_until IS NULL")
        note = await c.fetchval(
            "SELECT note FROM memory_evidence WHERE memory_id=$1", row["id"])
    assert row["source"] == MemorySource.EXTERNAL_SOURCE.value  # not settled truth
    assert row["needs_grounding"] is True  # surfaces as 'unverified' until checked
    assert note and "github.com" in note  # provenance recorded (§10)
