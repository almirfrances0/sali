"""Cognitive OS — the unified derived CognitiveState, the one optional subagent, user-clarification
state, proactive agent messages, and the capability registry. The runtime owns truth; the model reasons.
Lifecycle + capstone tests prove the system behaves as one organism across the whole interaction arc."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.events.publisher import EventPublisher
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime import cognitive
from sali.runtime.loop import AgentLoop
from sali.runtime.runtime import AgentRuntime
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.coordination import QuestionStore
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.store import TaskStore
from sali.tasks.watchdog import WatchdogConfig
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())
    pub = EventPublisher(pool)
    for st in (loop._tasks, loop._decisions, loop._phases, loop._research_store, loop._skills,
               loop._questions):
        st._publisher = pub
    loop._reviewer = TaskReviewer(pool, pub)
    loop._tasks._reviewer = loop._reviewer
    return loop


def _runtime(pool: Any, provider: FakeModelProvider) -> AgentRuntime:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())
    return AgentRuntime(loop, session_id=uuid4(), pool=pool,
                        watchdog_config=WatchdogConfig(check_interval=timedelta(seconds=1)))


async def _clear_lease(pool: Any) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE execution_lease SET status='releasing' WHERE id='foreground'")


@pytest.fixture(autouse=True)
async def _reset_lease(live_pool: Any) -> Any:
    await _clear_lease(live_pool)
    yield
    await _clear_lease(live_pool)


# ── CognitiveState: derived + reconstructable (§3/§4) ───────────────────────────────────────────────

async def test_cognitive_state_is_derived_and_reconstructable(live_pool: Any, tmp_path: Any) -> None:
    loop = _loop(live_pool, FakeModelProvider())
    store = loop._tasks
    task = await store.create("Build a Laravel app", ["scaffold", "auth"])
    await store.activate(task.id)
    await store.bind_workspace(task.id, objective="Build a Laravel app", explicit=None,
                               # A tmp root, not Almir's real one: binding an 'auto' workspace CREATES
                               # <root>/tasks/<id>/ on disk, so this test was leaving a folder in his
                               # actual archive on every run. The assertion below only needs the path to
                               # contain "sali-works", which a tmp copy does.
                               sali_works_root=str(tmp_path / "sali-works"))
    await loop._decisions.record(task.id, decision="Use PostgreSQL", source="user")

    state = await cognitive.assemble(live_pool, loop=loop, session_id=uuid4())
    snap = state.snapshot()
    assert snap["task_id"] == str(task.id) and snap["objective"] == "Build a Laravel app"
    assert snap["workspace"] and "sali-works" in snap["workspace"]
    assert snap["next_action"] and "capabilities" in snap and snap["capabilities"]
    assert "tasks" in snap["capabilities"] and "clarification" in snap["capabilities"]

    # destroy the in-memory state and reconstruct from PostgreSQL alone → same operational snapshot (§50)
    state2 = await cognitive.assemble(live_pool, loop=_loop(live_pool, FakeModelProvider()),
                                      session_id=uuid4())
    a, b = state.snapshot(), state2.snapshot()
    for k in ("task_id", "objective", "workspace", "next_action", "task_status"):
        assert a[k] == b[k]  # deterministic reconstruction


async def test_conversation_has_no_task_state(live_pool: Any) -> None:
    # §6: with no primary task, the cognitive state has no task — a plain conversation, nothing created.
    loop = _loop(live_pool, FakeModelProvider())
    state = await cognitive.assemble(live_pool, loop=loop, session_id=uuid4())
    snap = state.snapshot()
    assert snap["task_id"] is None and snap["objective"] is None and snap["next_action"] == ""


def test_capability_registry_lists_what_sali_can_do(live_pool: Any) -> None:
    caps = {c["capability"] for c in cognitive.capability_registry(default_registry())}
    assert {"filesystem", "shell", "web_research", "tasks", "clarification"} <= caps
    assert "delegation" not in caps   # single executive agent — no subagents (Prompt 12 §7)


# ── single executive agent — NO subagent (Prompt 12 §7 + user directive) ─────────────────────────────
# Brain-audit turn 8 removed the inert `Delegate` tool + `_DelegateSink` + `DelegationStore` scaffolds.
# The invariants they proved (no second reasoning stream, no delegate tool advertised) are now enforced
# by absence: there is no delegate tool CLASS to register, no ToolContext channel to pass one through,
# no `delegation` row that anything writes to. The remaining guard is a positive check that the tool
# registry has no delegate entry (belt-and-suspenders — the class doesn't exist, so registration would
# fail with an ImportError). The AST-based scaffold-removal tests live in
# tests/test_brain_turn8_delegation_scaffold_removed.py.

def test_no_delegate_tool_is_advertised() -> None:
    """The tool registry must not advertise any tool named 'delegate'."""
    assert "delegate" not in {t.name for t in default_registry().advertise()}


# ── user clarification: waiting_for_user (§44) ──────────────────────────────────────────────────────

async def test_ask_user_pauses_and_answer_resumes_same_task(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("Deploy the app", ["choose target", "deploy"])
    await store.activate(task.id)
    q = QuestionStore(live_pool, pub)
    qid = await q.ask(task.id, "Deploy to A or B?")
    paused = await store.get(task.id)
    assert paused is not None and paused.status == "waiting_for_user"   # legitimate pause, not failure
    pending = await q.pending(task.id)
    assert pending is not None and str(pending["id"]) == str(qid)
    # the user's answer resumes the SAME task
    assert await q.answer(task.id, "Use B") is True
    resumed = await store.get(task.id)
    assert resumed is not None and resumed.status == "running" and resumed.id == task.id
    assert await q.pending(task.id) is None                            # question resolved
    async with live_pool.acquire() as c:
        waited = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='task.waiting_for_user' AND subject_id=$1", task.id)
    assert waited >= 1


async def test_handle_message_answers_pending_question_and_resumes(live_pool: Any) -> None:
    # the runtime entry point (§5) records a reply to a pending question and resumes before classifying.
    rt = _runtime(live_pool, FakeModelProvider(responses=[
        ChatResult("Deploying to B now.", None, [], 3, 3, "fake")]))
    store = rt._loop._tasks
    task = await store.create("Deploy the app", ["deploy"])
    await store.activate(task.id)
    await rt._loop._questions.ask(task.id, "A or B?")
    w = await store.get(task.id)
    assert w is not None and w.status == "waiting_for_user"

    await rt.handle_message("Use B", origin="cli")
    # the pending question is answered (the task was resumed by the runtime before classification)
    async with live_pool.acquire() as c:
        answered = await c.fetchval(
            "SELECT status FROM task_question WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", task.id)
    assert answered == "answered"


# ── proactive agent-originated messages (§34/§35) ───────────────────────────────────────────────────

async def test_send_agent_message_emits_a_distinct_event(live_pool: Any) -> None:
    rt = _runtime(live_pool, FakeModelProvider())
    await rt.send_agent_message("The website build passed verification.", importance="completion")
    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT payload FROM event WHERE event_type='agent.message' ORDER BY seq DESC LIMIT 1")
    assert row is not None
    p = dict(row["payload"])
    assert p["channel"] == "agent_message" and p["importance"] == "completion"
    assert "passed verification" in p["text"]


# ── observability snapshot (§46) ────────────────────────────────────────────────────────────────────

async def test_runtime_snapshot_is_operational_only(live_pool: Any) -> None:
    rt = _runtime(live_pool, FakeModelProvider())
    store = rt._loop._tasks
    task = await store.create("Build it", ["a"])
    await store.activate(task.id)
    snap = await rt.snapshot()
    # a compact operational view — no chain-of-thought fields
    assert snap["task_id"] == str(task.id) and "next_action" in snap and "capabilities" in snap
    assert not any(k in snap for k in ("thoughts", "reasoning", "chain_of_thought", "prompt"))
