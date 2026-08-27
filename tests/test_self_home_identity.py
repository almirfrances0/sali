"""Canonical self/home/environment identity (the correction directive §1-§14).

Deterministic checks that the ARCHITECTURE makes "this host is my home/body" true, keeps
IDENTITY / SELF-STATE / WORLD-STATE distinct, lets live/observed beat inferred at read time, and routes
system/environment questions to current reality over stale memory. No LLM plausibility is judged — these
verify the retrieved facts and the assembled context, per §14.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.context.engine import ContextEngine
from sali.core.enums import MemoryLayer, MemorySource
from sali.graph.writer import ensure_node
from sali.memory import retriever
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider
from sali.retrieval.models import RetrievalBundle
from sali.retrieval.router import classify
from sali.runtime.self_state import SelfStateStore
from sali.twin.observers import observe_machine
from sali.twin.self_link import link_self

pytestmark = pytest.mark.db


# ── §1: this host is structurally Sali's home/body ──────────────────────────────────────────────────
def test_observe_machine_marks_self_and_home() -> None:
    key, _name, props = observe_machine()
    assert key.startswith("machine:")
    assert props["is_self"] == "true" and props["role"] == "home"  # not prose — a structural marker


async def test_lives_on_edge_and_env_reports_home(live_pool: Any) -> None:
    async with live_pool.acquire() as conn, conn.transaction():
        await ensure_node(conn, node_type="person", name="Almir", canonical_key="person:almir",
                          source=MemorySource.USER_EXPLICIT)
        machine = await ensure_node(
            conn, node_type="machine", name="Kali GNU/Linux Rolling", canonical_key="machine:testhost",
            source=MemorySource.SYSTEM_OBSERVATION,
            props={"hostname": "testhost", "os": "Kali GNU/Linux Rolling", "kernel": "7.0",
                   "arch": "x86_64", "is_self": "true", "role": "home", "machine_id": "abc123"})
        await link_self(conn, machine_id=machine.id, model_name="sali:latest",
                        workspace="/home/almir/Desktop/sali-works", source_dir="/home/almir/Desktop/sali")
        rel = await conn.fetchval(
            "SELECT e.rel_type FROM graph_edge e JOIN graph_node a ON a.id=e.src_id "
            "WHERE a.canonical_key='agent:sali' AND e.rel_type='lives_on' AND e.valid_until IS NULL")
    assert rel == "lives_on"  # agent:sali --lives_on--> machine is a real graph fact now

    view = await SelfStateStore(live_pool).assemble()
    env = view["environment"]
    assert env.get("is_home") is True  # SELF-STATE knows this host is its own machine
    assert env.get("machine") == "Kali GNU/Linux Rolling"
    assert env.get("machine_id") == "abc123"  # stable identity surfaced, survives a rename


# ── §4/§11: a system/environment question routes to live reality, not semantic memory ─────────────────
def test_system_queries_classify_as_system_and_live() -> None:
    for q in ("what computer am I on?", "where do I live?", "analyse the system",
              "what GPU do I have?", "what services are currently running?", "what OS am I on",
              "check your PC", "audit this machine", "my disk usage"):
        plan = classify(q)
        assert plan.system_query, f"not a system query: {q!r}"
        assert plan.needs_live, f"system query didn't force live: {q!r}"


def test_non_system_query_stays_normal() -> None:
    plan = classify("what did I say about the deployment plan last week?")
    assert not plan.system_query


# ── §11: self-state + health are now IN the assembled context, distinct from identity/world ───────────
def _engine() -> ContextEngine:
    return ContextEngine(FakeModelProvider(), ctx_tokens=8192)


def test_self_and_health_sections_are_assembled() -> None:
    ctx = _engine().assemble(
        "how are you", RetrievalBundle(), [],
        self_note="Myself (self-state):\nThis host is my home.",
        health_note="My faculties right now: all good.")
    assert "self" in ctx.included and "health" in ctx.included
    text = ctx.messages[0].content
    assert "my home" in text and "faculties" in text


async def test_system_query_demotes_memory_below_live(live_pool: Any) -> None:
    # seed a semantic memory, retrieve it, then assemble a SYSTEM query with a live world_note present.
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="months ago the disk was 40% full",
                       source=MemorySource.INFERENCE)
    hits = await mem.retrieve("disk", k=5)
    bundle = RetrievalBundle(memories=hits)

    normal = _engine().assemble("tell me about the disk history", bundle, [],
                                world_note="World: disk 12% full now.", system_query=False)
    system = _engine().assemble("analyse the system", bundle, [],
                                world_note="World: disk 12% full now.", system_query=True,
                                self_note="Myself: this host is my home.")
    # both include world above memory; the system query additionally DEMOTES memory beneath the live band
    assert normal.included.index("world") < normal.included.index("memories")
    assert system.included.index("world") < system.included.index("memories")
    assert system.included.index("self") < system.included.index("memories")


# ── §3: at read time, an OBSERVED claim outranks an INFERRED one of equal relevance ───────────────────
async def test_observation_outranks_inference(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="datastore engine here is postgresql",
                       source=MemorySource.SYSTEM_OBSERVATION)
    await mem.embed_pending()  # keyword retriever only returns embed_status='done' rows
    async with live_pool.acquire() as conn:
        rows = await retriever.retrieve_keyword(conn, "datastore", 5)
    assert rows, "seed not retrievable"
    row = dict(rows[0])
    # identical hit, differing ONLY in source — the observation must score strictly higher (source precedence)
    entry_obs = {"row": {**row, "source": "system_observation"}, "similarity": 0.8, "retrievers": {"vector"}}
    entry_inf = {"row": {**row, "source": "inference"}, "similarity": 0.8, "retrievers": {"vector"}}
    from sali.core.clock import SystemClock
    now = SystemClock().now()
    assert mem._to_hit(entry_obs, now).score > mem._to_hit(entry_inf, now).score


# ── §9: a remote host is a DISTINCT entity Sali can_access — never the home machine ──────────────────
async def test_reach_host_makes_distinct_remote_node(live_pool: Any) -> None:
    from sali.graph.service import GraphService

    g = GraphService(live_pool)
    await g.reach_host("root@vps-01.example.com")
    async with live_pool.acquire() as conn:
        host = await conn.fetchrow(
            "SELECT node_type, props->>'is_self' AS is_self, props->>'role' AS role FROM graph_node "
            "WHERE canonical_key LIKE 'host:%' AND valid_until IS NULL")
        edge = await conn.fetchval(
            "SELECT e.rel_type FROM graph_edge e JOIN graph_node a ON a.id=e.src_id "
            "WHERE a.canonical_key='agent:sali' AND e.rel_type='can_access' AND e.valid_until IS NULL")
    assert host is not None and host["node_type"] == "host"
    assert host["is_self"] == "false" and host["role"] == "remote"  # NOT the home machine
    assert edge == "can_access"


# ── §7: live host resources are folded into the world snapshot when asked ─────────────────────────────
async def test_world_snapshot_with_resources(live_pool: Any) -> None:
    from sali.runtime.world_state import WorldStateBuilder

    ws = await WorldStateBuilder(live_pool).snapshot(with_resources=True)
    # /proc-backed readings are always available on Linux; gpu is optional (may be None without a GPU)
    assert ws.cpu_mem and "MiB" in ws.cpu_mem
    assert ws.disk and "GiB" in ws.disk
    assert "Memory:" in ws.render() and "Disk:" in ws.render()
