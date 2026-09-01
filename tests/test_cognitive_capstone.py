"""Cognitive OS capstone (§48) — the intended Sali behaviour, end to end: a task is created, planned,
worked, researched, a subagent is delegated, the context compacts, the user interrupts then clarifies,
the process restarts and the CognitiveState is reconstructed from durable state, the reviewer requires
rework, Sali fixes it, and the task completes — all as ONE logical task with one stable identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.events.publisher import EventPublisher
from sali.provider.base import ChatMessage, ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime import cognitive, context_budget
from sali.runtime.loop import AgentLoop, _DelegateSink
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.skills.store import SkillStore
from sali.tasks.coordination import QuestionStore
from sali.tasks.ledger import DecisionStore
from sali.tasks.research import ResearchStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db

_REPO_SKILLS = str(Path(__file__).resolve().parents[1] / "skills")


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))
    pub = EventPublisher(pool)
    for st in (loop._tasks, loop._skills, loop._research_store, loop._decisions, loop._phases,
               loop._delegations, loop._questions):
        st._publisher = pub
    loop._reviewer = TaskReviewer(pool, pub)
    loop._tasks._reviewer = loop._reviewer
    return loop


async def test_cognitive_os_capstone(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[ChatResult("Subagent: the API needs Node 22.", None, [], 3, 3, "fake")])
    loop = _loop(live_pool, fake)
    store = loop._tasks
    budget = context_budget.resolve_limit(fake, loop.settings)
    assert budget == 24_576  # the small operational working set

    # 1) task + workspace + skills + plan + decision + phase (all durable)
    task = await store.create("Build a Laravel application with auth and tests",
                              ["scaffold", "auth", "tests"])
    await store.activate(task.id)
    original_id = task.id
    bound = await store.bind_workspace(
        task.id, objective="Build a Laravel application", explicit=None,
        sali_works_root="/home/almir/Desktop/sali-works")
    workspace = bound["workspace_root"]
    await SkillStore(live_pool).select_and_persist(
        task.id, objective="Build a Laravel application", skills_root=_REPO_SKILLS)
    await loop._decisions.record(task.id, decision="Use PostgreSQL, not SQLite",
                                 reason="production requirement", source="user")
    await loop._phases.start(task.id, "Backend")

    # 2) work: an execution + a step done, and research the unknown
    ex = await store.record_execution(task.id, 1, "composer", tool_args={})
    await store.complete_execution(ex, status="completed", result_summary="scaffolded")
    async with live_pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1 AND seq=1", task.id)
    await loop._research_store.record_research(
        task_id=task.id, run_id=uuid4(), step_seq=2, query="Laravel Breeze auth",
        source="https://laravel.com/docs/breeze", summary="composer require laravel/breeze --dev",
        confidence=0.8, content_hash="h")

    # 3) delegation is DISABLED — Sali is a single executive agent (no subagent, no second model call);
    #    the request refuses and nothing is spawned, so the primary task is untouched.
    out = await _DelegateSink(loop=loop, tasks=store, run_id=uuid4(),
                              store=loop._delegations).delegate("check the API's Node requirement")
    assert out["ok"] is False and await loop._delegations.list_for_task(task.id) == []

    # 4) compaction: the fold carries the deterministic capsule (objective + NEXT ACTION + decision),
    #    bounded under the 24K budget — no summary-of-summary chain
    msgs = [ChatMessage(role="system", content="S"), ChatMessage(role="user", content="build it")]
    msgs += [ChatMessage(role="assistant", content="chatter " * 300) for _ in range(8)]
    for _ in range(4):
        folded = await loop._fold_messages(msgs)
        carried = "\n".join(m.content or "" for m in folded)
        assert "Build a Laravel application" in carried and "NEXT ACTION" in carried
        assert "Use PostgreSQL" in carried
        assert context_budget.estimate_tokens(fake, folded) < budget
        msgs = folded + [ChatMessage(role="assistant", content="more " * 300) for _ in range(8)]

    # 5) interrupt → suspend → resume, then the user is asked to clarify and answers → same task
    await store.suspend(task.id, reason="quick question")
    r0 = await store.resume(task.id)
    assert r0 is not None and r0.id == original_id
    q = QuestionStore(live_pool, EventPublisher(live_pool))
    await q.ask(task.id, "Deploy to staging or prod?")
    w0 = await store.get(task.id)
    assert w0 is not None and w0.status == "waiting_for_user"
    assert await q.answer(task.id, "staging") is True
    w1 = await store.get(task.id)
    assert w1 is not None and w1.status == "running"

    # 6) PROCESS RESTART: a brand-new loop/stores; reconstruct the CognitiveState from PostgreSQL alone
    loop2 = _loop(live_pool, FakeModelProvider())
    state = await cognitive.assemble(live_pool, loop=loop2, session_id=uuid4())
    snap = state.snapshot()
    assert snap["task_id"] == str(original_id)                 # SAME task_id
    assert snap["workspace"] == workspace                      # SAME workspace
    assert snap["phase"] == "Backend"                          # phase preserved
    assert "laravel" in snap["selected_skills"]                # skills preserved
    assert snap["research_count"] >= 1                         # research preserved
    assert snap["next_action"]                                 # knows what to do next
    d2 = DecisionStore(live_pool)
    assert [d["decision"] for d in await d2.active(original_id)] == ["Use PostgreSQL, not SQLite"]
    r2 = ResearchStore(live_pool)
    assert (await r2.list_research(original_id))[0]["summary"] == "composer require laravel/breeze --dev"

    # 7) reviewer NEEDS_REWORK (steps 2,3 still open) → fix → PASS → complete
    reviewer = TaskReviewer(live_pool)
    assert (await reviewer.review(original_id)).status is ReviewStatus.NEEDS_REWORK
    async with live_pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", original_id)
        n_exec = await c.fetchval("SELECT count(*) FROM task_execution WHERE task_id=$1", original_id)
        n_distinct = await c.fetchval(
            "SELECT count(DISTINCT (step_seq, tool_name, attempt)) FROM task_execution WHERE task_id=$1",
            original_id)
    assert n_exec == n_distinct  # no duplicated executions
    err = await loop2._tasks.finish(original_id, status="done")
    assert err is None and await loop2._tasks.get(original_id) is None  # PASS → completed + archived

    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM task WHERE id=$1", original_id) == 0  # one task, gone
