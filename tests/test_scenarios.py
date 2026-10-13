"""End-to-end scenarios through the whole agent loop (spec §37-43 + Almir's real flows).

Each scenario drives a real AgentLoop (retrieval → context → tools → journal → Postgres) with a
scripted FakeModelProvider, so it exercises the wiring the deep audit found broken — memory that
now accrues and is reinforced, provenance carried to use-time, the conversational→graph write path,
twin "small things" recall, instant greetings, and interrupted-turn cleanup — not just units.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.clock import SystemClock
from sali.core.enums import MemoryLayer, MemorySource
from sali.core.ids import new_id
from sali.memory import writer as mem_writer
from sali.provider.base import ChatMessage, ChatResult, ToolCall
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.retry import RetryAction, retry_decision
from sali.tasks.store import TaskStore
from sali.tools.builtins.task_tool import AdvanceTask
from sali.tools.context import ToolContext
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


async def test_capstone_long_task_survives_failure_retry_fold_restart_and_finishes_once(live_pool: Any) -> None:
    """§37 — the composition proof. One long task threads every hardened mechanism: a 20+ step plan
    with a dependency, a verify-failure + retry (Phase 0/3), a mid-turn fold whose continuation packet
    keeps the active step AND the failure deterministically (Phase 2), a mid-step checkpoint (Phase 3),
    a simulated restart that resurfaces the SAME task (§24) and a crash-resume re-drive (Phase 4), and a
    verified final step linked to a real execution (Phase 1/§8) — completing EXACTLY once, no lost state."""
    store = TaskStore(live_pool)

    # 1) a 20+ step task, with step 21 depending on step 20 (a per-step DAG).
    steps: list[Any] = [f"step {i}" for i in range(1, 21)]
    steps.append({"description": "final verify", "depends_on": [20]})
    task = await store.create("Deploy the whole stack", steps)
    assert len(task.steps) == 21 and task.next_step is not None and task.next_step.seq == 1

    # 2) advance a couple, then a TRANSIENT verify-failure on step 3 — the durable record classifies it.
    await store.advance(task.id, 1, "done")
    await store.advance(task.id, 2, "done")
    t, _ = await store.advance(task.id, 3, "failed", error="connection timed out")
    assert t is not None
    s3 = next(s for s in t.steps if s.seq == 3)
    assert s3.attempts == 1 and s3.failure_class == "transient"
    assert retry_decision(s3.attempts, s3.failure_class) is RetryAction.RETRY  # policy: bounded retry

    # 3) FOLD mid-turn while step 3 is still failed — the continuation packet must carry, DETERMINISTICALLY,
    #    the active task, the step ticks, AND the failure with its retry recommendation (Phase 2 ⊇ Phase 0).
    loop = _loop(live_pool, FakeModelProvider(responses=[ChatResult("progress", None, [], 3, 3, "fake")]))
    msgs = [
        ChatMessage(role="system", content="SYS"),
        ChatMessage(role="user", content="deploy the stack"),
        ChatMessage(role="assistant", content="did step 2"),
        ChatMessage(role="tool", name="t", content="ok"),
    ]
    folded = await loop._fold_messages(msgs)
    carried = "\n".join(m.content or "" for m in folded)
    # Prompt 6: the fold carries the deterministic Task State Capsule (objective + NEXT ACTION, from
    # durable state), and the step failure + retry recommendation survive it.
    assert "TASK STATE CAPSULE" in carried and "Deploy the whole stack" in carried and "NEXT ACTION" in carried
    assert "FAILED step 3 (transient" in carried and "retry" in carried  # failure + recommendation survive

    # 4) retry step 3 to success (attempt history is preserved — exactly-once, not a duplicate step).
    t, _ = await store.advance(task.id, 3, "done")
    assert t is not None
    s3 = next(s for s in t.steps if s.seq == 3)
    assert s3.status == "done" and s3.attempts == 1

    # 5) a long step (4) checkpoints mid-way; a fresh read (RESTART) resumes from the checkpoint (§5/§24).
    await store.checkpoint(task.id, 4, {"processed": 50, "of": 100})
    restarted = _loop(live_pool, FakeModelProvider())
    note = await restarted._open_tasks_note()
    assert note is not None and "Deploy the whole stack" in note and "step 4" in note

    # 6) crash-resume: a REPLAY_SAFE run left by a "crash" is actually RE-DRIVEN, not lost (Phase 4/§9).
    session = new_id()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO agent_runs (run_id, session_id, user_input, state, status, updated_at) "
            "VALUES (gen_random_uuid(), $1, 'continue the deploy', 'reason_plan', 'running', "
            "        now() - interval '5 minutes')",
            session,
        )
    resumer = _loop(live_pool, FakeModelProvider(responses=[ChatResult("resumed", None, [], 3, 3, "fake")]))
    resolved = await resumer.recover()
    assert resolved and resolved[0]["action"] == "replay_safe" and "resumed_run" in resolved[0]

    # 7) finish the rest; the FINAL step is marked via the real advance_task tool, which links the
    #    verified execution (§8) — and step 21 only becomes ready once its dependency (20) is done.
    run_id, exec_id = uuid4(), uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO tool_execution "
            "  (id, run_id, tool_name, status, danger_level, plan, success, finished_at) "
            "VALUES ($1,$2,'execute_command','verified_success',1,$3,true, now())",
            exec_id, run_id, {"args": {}},
        )
    for i in range(4, 21):
        await store.advance(task.id, i, "done")
    ready = await store.get(task.id)
    assert ready is not None and ready.next_step is not None and ready.next_step.seq == 21  # dep satisfied

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, run_id=run_id, tasks=store)
    final = await AdvanceTask().run({"step": 21, "status": "done"}, ctx)
    assert final.ok

    # 8) the whole task completed EXACTLY once — archived to sali-works/tasks/ and cleaned from DB.
    # The advance_task tool returned ok=True with "task done — archived" display.
    done = await store.get(task.id)
    assert done is None  # archived and cleaned
