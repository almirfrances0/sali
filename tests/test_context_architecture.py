"""High-performance context architecture (Prompt 6) — the operational 24K budget, the deterministic
Task State Capsule (reconstructed from durable state), decision ledger, negative knowledge + repeated
failure detection, retrieval, phases, and emergency mode. Operational state is lossless because it is
reconstructed from structured storage; only prose is compressed."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.events.publisher import EventPublisher
from sali.provider.fake import FakeModelProvider
from sali.runtime import capsule as capmod
from sali.runtime import context_budget
from sali.runtime.capsule import build_capsule, render_capsule
from sali.tasks import history
from sali.tasks.ledger import DecisionStore, PhaseStore
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


# ── the operational budget (§3/§17/§18) ─────────────────────────────────────────────────────────────

def test_operational_budget_defaults_to_24k_and_caps_the_model_window() -> None:
    s = _settings()
    assert s.runtime.context_limit == 24_576 and s.runtime.output_reserve == 3_072
    # the model window is 32K but Sali works in 24K — the small working set (§3)
    assert context_budget.resolve_limit(FakeModelProvider(), s) == 24_576
    assert context_budget.output_reserve(s) == 3_072


def test_output_reserve_is_held_back_from_usable(live_pool: Any) -> None:
    from sali.provider.base import ChatMessage
    b = context_budget.budget_for(FakeModelProvider(), _settings(),
                                  [ChatMessage(role="user", content="hi")])
    assert b.limit == 24_576 and b.usable == 24_576 - 3_072  # output reservation respected


# ── the Task State Capsule (§11/§12/§24) ────────────────────────────────────────────────────────────

async def _rich_task(pool: Any, pub: Any) -> Any:
    store = TaskStore(pool, pub)
    task = await store.create("Build the Laravel app", ["scaffold", "auth", "deploy"])
    await store.activate(task.id)
    await store.bind_workspace(task.id, objective="Build the Laravel app", explicit=None,
                               sali_works_root="/home/almir/Desktop/sali-works")
    # step 1 done+verified, step 2 failed (transient)
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1 AND seq=1", task.id)
        await c.execute("UPDATE task_step SET status='failed', failure_class='transient', "
                        "last_error='connection reset' WHERE task_id=$1 AND seq=2", task.id)
    ex = await store.record_execution(task.id, 1, "create_file", tool_args={})
    await store.complete_execution(ex, status="completed", result_summary="scaffolded composer.json")
    return await store.get(task.id)


async def test_capsule_reconstructs_operational_state_from_durable_storage(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    task = await _rich_task(live_pool, pub)
    dec = DecisionStore(live_pool, pub)
    await dec.record(task.id, decision="Use PostgreSQL, not SQLite", reason="production requirement",
                     source="user")
    cap = await build_capsule(live_pool, task, decisions=dec, reviewer=TaskReviewer(live_pool))
    text = render_capsule(cap)
    assert "TASK STATE CAPSULE" in text
    assert "Build the Laravel app" in text and str(task.id) in text
    assert "/home/almir/Desktop/sali-works" in text                     # workspace
    assert "Use PostgreSQL, not SQLite" in text and "[user]" in text    # decision survives
    assert "FAILED step 2 (transient" in text                           # step failure detail
    assert "LAST SUCCESS: create_file" in text                          # last verified execution
    assert "NEXT ACTION" in text                                        # anchored next action


async def test_capsule_next_action_is_deterministic_and_anchored(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("Build the app", ["a", "b"])
    await store.activate(task.id)
    cap = await build_capsule(live_pool, await store.get(task.id))
    assert cap.next_action.startswith("Do step 1")  # next ready step
    # all steps done → next action is to run the reviewer
    async with live_pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task.id)
    cap2 = await build_capsule(live_pool, await store.get(task.id))
    assert "finish_task" in cap2.next_action


async def test_capsule_reproducible_after_restart(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    task = await _rich_task(live_pool, pub)
    a = render_capsule(await build_capsule(live_pool, task))
    # a fresh set of stores (as after a restart) reconstructs the same capsule from PostgreSQL
    b = render_capsule(await build_capsule(live_pool, await TaskStore(live_pool).get(task.id)))
    assert a == b  # deterministic, reproducible (§24)


# ── decision ledger (§30) ───────────────────────────────────────────────────────────────────────────

async def test_decision_ledger_supersedes_contradictory_decisions(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("x", ["a"])
    dec = DecisionStore(live_pool, pub)
    d1 = await dec.record(task.id, decision="Use SQLite", reason="quick start", source="sali")
    assert [d["decision"] for d in await dec.active(task.id)] == ["Use SQLite"]
    await dec.record(task.id, decision="Use PostgreSQL", reason="prod", source="user", supersedes=d1)
    active = await dec.active(task.id)
    assert [d["decision"] for d in active] == ["Use PostgreSQL"]  # only one active, no contradiction
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='task.decision_superseded' AND subject_id=$1",
            task.id)
    assert n >= 1


# ── negative knowledge + repeated failure (§29/§44/§45) ─────────────────────────────────────────────

async def test_negative_knowledge_and_repeated_failure_detection(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("x", ["a"])
    for i in range(3):  # the same command fails three times
        ex = await store.record_execution(task.id, 1, "npm_install", tool_args={}, attempt=i + 1)
        await store.complete_execution(ex, status="failed", error="requires Node >= 22")
    fails = await history.recent_failures(live_pool, task.id)
    assert fails and fails[0]["error"] == "requires Node >= 22"
    repeated = await history.repeated_failures(live_pool, task.id, threshold=3)
    assert repeated and repeated[0]["tool_name"] == "npm_install" and repeated[0]["n"] == 3
    cap = await build_capsule(live_pool, await store.get(task.id))
    text = render_capsule(cap)
    assert "NEGATIVE KNOWLEDGE" in text and "requires Node >= 22" in text
    assert "REPEATED FAILED ACTION" in text  # nudged to research/alternative, not loop


async def test_relevant_execution_retrieval_is_targeted(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("x", ["a", "b"])
    ex1 = await store.record_execution(task.id, 1, "npm_build", tool_args={})
    await store.complete_execution(ex1, status="failed", error="ModuleNotFoundError: left-pad")
    ex2 = await store.record_execution(task.id, 2, "psql", tool_args={})
    await store.complete_execution(ex2, status="completed")
    # retrieving on the current error finds the matching prior failure, not the unrelated psql run
    rel = await history.relevant_executions(live_pool, task.id, error="ModuleNotFoundError: left-pad")
    assert [r["tool_name"] for r in rel] == ["npm_build"]
    assert await history.relevant_executions(live_pool, task.id, tool="does_not_exist") == []


# ── phases (§31/§32) ────────────────────────────────────────────────────────────────────────────────

async def test_phases_start_complete_and_summarize(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("big task", ["a"])
    phases = PhaseStore(live_pool, pub)
    p1 = await phases.start(task.id, "Backend")
    cur1 = await phases.current(task.id)
    assert p1["seq"] == 1 and cur1 is not None and cur1["name"] == "Backend"
    p2 = await phases.start(task.id, "Frontend", summary_of_prior="backend API done + tested")
    cur2 = await phases.current(task.id)
    assert p2["seq"] == 2 and cur2 is not None and cur2["name"] == "Frontend"
    all_phases = await phases.phases(task.id)
    backend = next(p for p in all_phases if p["name"] == "Backend")
    assert backend["status"] == "done" and backend["summary"] == "backend API done + tested"
    async with live_pool.acquire() as c:
        started = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='task.phase_started' AND subject_id=$1", task.id)
    assert started >= 2


# ── emergency mode (§42) ────────────────────────────────────────────────────────────────────────────

async def test_emergency_capsule_is_minimal_but_actionable(live_pool: Any) -> None:
    from sali.tasks.ledger import PhaseStore
    from sali.tasks.research import ResearchStore
    pub = EventPublisher(live_pool)
    task = await _rich_task(live_pool, pub)
    research = ResearchStore(live_pool, pub)
    await research.record_research(task_id=task.id, run_id=None, step_seq=2, query="node version",
                                   source="https://nodejs.org", summary="needs Node 22",
                                   confidence=0.7, content_hash="h")
    phases = PhaseStore(live_pool, pub)
    await phases.start(task.id, "Backend")
    await phases.start(task.id, "Frontend", summary_of_prior="backend done")  # a completed phase
    cap = await build_capsule(live_pool, task, research=research, phases=phases)
    full = render_capsule(cap)
    emergency = render_capsule(cap, emergency=True)
    # emergency drops research + completed-phase summaries → strictly smaller
    assert len(emergency) < len(full)
    assert "RESEARCH FINDINGS" in full and "RESEARCH FINDINGS" not in emergency
    # …but still carries the essentials to continue
    assert "Build the Laravel app" in emergency and "NEXT ACTION" in emergency
    assert cap.workspace_root and cap.workspace_root in emergency


# ── context checkpoint / manifest (§23/§25) ─────────────────────────────────────────────────────────

class _FoldingProvider(FakeModelProvider):
    """Drives a few tool cycles under a tiny window so the loop compacts, then finishes."""

    def __init__(self, *, tool_iters: int, ctx_limit: int) -> None:
        super().__init__(ctx_limit=ctx_limit)
        self._left = tool_iters

    async def chat(self, messages: Any, **kw: Any) -> Any:
        from sali.provider.base import ChatResult
        return ChatResult("DONE: progress folded.", None, [], 3, 3, "fake")

    async def chat_stream(self, messages: Any, **kw: Any) -> Any:
        from sali.provider.base import ChatChunk, ChatResult, ToolCall
        if self._left > 0:
            self._left -= 1
            res = ChatResult("", None, [ToolCall("memory_info", {})], 5, 5, "fake")
        else:
            res = ChatResult("All done.", None, [], 5, 5, "fake")
        yield ChatChunk(done=True, result=res)


async def test_compaction_writes_a_context_checkpoint_and_emits(live_pool: Any) -> None:
    from sali.context.engine import ContextEngine
    from sali.retrieval.service import RetrievalService
    from sali.runtime.loop import AgentLoop
    from sali.security.confirm import AutoAllowConfirmer
    from sali.security.policy import PolicyEngine
    from sali.tools.registry import default_registry

    prov = _FoldingProvider(tool_iters=3, ctx_limit=400)  # tiny operational window → compaction
    loop = AgentLoop(
        pool=live_pool, provider=prov, retrieval=RetrievalService(live_pool, prov),
        context=ContextEngine(prov, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())
    pub = EventPublisher(live_pool)
    loop._publisher = pub
    for st in (loop._tasks, loop._decisions, loop._phases, loop._research_store, loop._skills):
        st._publisher = pub
    store = loop._tasks
    task = await store.create("Build the app", ["a", "b"])
    await store.activate(task.id)
    await store.bind_workspace(task.id, objective="Build the app", explicit=None,
                               sali_works_root="/home/almir/Desktop/sali-works")

    await loop.run("keep going on the app build")

    async with live_pool.acquire() as c:
        checkpoints = await c.fetchval(
            "SELECT count(*) FROM context_checkpoint WHERE task_id=$1", task.id)
        cp_events = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='context.checkpoint_created' AND subject_id=$1",
            task.id)
        assembled = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='context.assembled' AND subject_id=$1", task.id)
    assert checkpoints >= 1 and cp_events >= 1 and assembled >= 1  # manifest written + observable


async def test_capsule_source_ids_form_a_reproducible_manifest(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    task = await _rich_task(live_pool, pub)
    dec = DecisionStore(live_pool, pub)
    await dec.record(task.id, decision="Use PostgreSQL", source="user")
    cap = await build_capsule(live_pool, task, decisions=dec)
    ids = cap.source_ids()
    assert set(ids) == {"research", "executions", "decisions", "skills"}
    assert ids["decisions"]  # the decision id is captured in the manifest
    assert capmod.CONTEXT_VERSION == 1
