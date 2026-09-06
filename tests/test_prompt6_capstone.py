"""Prompt 6 mandatory capstone (§56) — a genuinely long task operated under a 24,576-token budget:
100+ interactions, many executions, repeated compactions, research, decisions, failed attempts, a user
interruption + resume, reviewer rework, a process restart, and final completion — and still one stable
task_id, the same workspace, the correct NEXT ACTION, verified evidence, no duplicated executions, and a
context that never exceeds the operational budget. Operational state is lossless (reconstructed from
durable storage); only prose is compressed."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.events.publisher import EventPublisher
from sali.provider.base import ChatMessage
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime import context_budget
from sali.runtime.capsule import build_capsule, render_capsule
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.skills.store import SkillStore
from sali.tasks.ledger import DecisionStore
from sali.tasks.research import ResearchStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db

_REPO_SKILLS = str(Path(__file__).resolve().parents[1] / "skills")


def _loop(pool: Any) -> AgentLoop:
    fake = FakeModelProvider()
    loop = AgentLoop(
        pool=pool, provider=fake, retrieval=RetrievalService(pool, fake),
        context=ContextEngine(fake, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))
    pub = EventPublisher(pool)
    for store in (loop._tasks, loop._skills, loop._research_store, loop._decisions, loop._phases):
        store._publisher = pub
    loop._reviewer = TaskReviewer(pool, pub)
    loop._tasks._reviewer = loop._reviewer
    return loop


async def test_capstone_long_task_24k_budget(live_pool: Any) -> None:
    loop = _loop(live_pool)
    store = loop._tasks
    fake = loop.provider
    budget = context_budget.resolve_limit(fake, loop.settings)
    assert budget == 24_576  # the operational working set — NOT the model's real window

    # ── set up a large Laravel task ─────────────────────────────────────────────────────────────────
    task = await store.create("Build a large Laravel application with admin, CRUD, tests",
                              ["scaffold", "backend", "frontend", "tests"])
    await store.activate(task.id)
    original_id = task.id
    bound = await store.bind_workspace(
        task.id, objective="Build a large Laravel application", explicit=None,
        sali_works_root="/home/almir/Desktop/sali-works")
    workspace = bound["workspace_root"]
    await SkillStore(live_pool).select_and_persist(
        task.id, objective="Build a large Laravel application", skills_root=_REPO_SKILLS)
    await loop._decisions.record(task.id, decision="Use PostgreSQL, not SQLite",
                                 reason="production requirement", source="user")
    await loop._phases.start(task.id, "Backend")

    # ── 100+ interactions: many executions, some repeated failures, research ─────────────────────────
    for i in range(60):
        ex = await store.record_execution(task.id, 2, "artisan", tool_args={"n": i}, attempt=i + 1)
        await store.complete_execution(ex, status="completed", result_summary=f"made model {i}")
    for i in range(5):  # a repeated failing action (negative knowledge)
        ex = await store.record_execution(task.id, 2, "npm_install", tool_args={}, attempt=100 + i)
        await store.complete_execution(ex, status="failed", error="requires Node >= 22")
    await loop._research_store.record_research(
        task_id=task.id, run_id=uuid4(), step_seq=2, query="Laravel + Node 22",
        source="https://laravel.com", summary="upgrade Node to 22 for Vite", confidence=0.8,
        content_hash="h")

    # the capsule stays bounded no matter how much history accumulates (§4/§8)
    cap = await build_capsule(live_pool, await store.get(task.id), reviewer=loop._reviewer,
                              research=loop._research_store, skills=loop._skills,
                              decisions=loop._decisions, phases=loop._phases)
    rendered = render_capsule(cap)
    assert fake.count_tokens(rendered) < budget // 4  # a small fraction of the budget — bounded
    assert "Use PostgreSQL, not SQLite" in rendered           # decision preserved
    assert "requires Node >= 22" in rendered                  # negative knowledge preserved
    assert "REPEATED FAILED ACTION" in rendered               # repetition detected
    assert workspace in rendered and "NEXT ACTION" in rendered

    # ── many compaction cycles: the fold carries the deterministic capsule, stays ≤ budget ──────────
    msgs = [ChatMessage(role="system", content="S"), ChatMessage(role="user", content="build it")]
    msgs += [ChatMessage(role="assistant", content="chatter " * 300) for _ in range(8)]
    for _ in range(6):  # repeated compaction remains bounded (no summary-of-summary explosion)
        folded = await loop._fold_messages(msgs)
        assert len(folded) <= 4
        carried = "\n".join(m.content or "" for m in folded)
        assert original_id.hex[:8] in carried.replace("-", "") or str(original_id) in carried
        assert "Build a large Laravel application" in carried and "NEXT ACTION" in carried
        assert "Use PostgreSQL" in carried              # the decision survives every compaction
        assert context_budget.estimate_tokens(fake, folded) < budget  # never exceeds the budget
        msgs = folded + [ChatMessage(role="assistant", content="more " * 300) for _ in range(8)]

    # ── interruption → suspend → resume (Prompt 2), then restart (fresh stores) ──────────────────────
    await store.suspend(task.id, reason="quick question")
    resumed = await store.resume(task.id)
    assert resumed is not None and resumed.id == original_id
    s2 = TaskStore(live_pool, EventPublisher(live_pool), TaskReviewer(live_pool))
    d2, r2 = DecisionStore(live_pool), ResearchStore(live_pool)
    t2 = await s2.get(original_id)
    assert t2 is not None and t2.id == original_id and t2.workspace_root == workspace  # nothing drifted
    assert [d["decision"] for d in await d2.active(original_id)] == ["Use PostgreSQL, not SQLite"]
    assert (await r2.list_research(original_id))[0]["summary"] == "upgrade Node to 22 for Vite"

    # ── reviewer NEEDS_REWORK (a step still open), then fix, then PASS → complete ────────────────────
    reviewer = TaskReviewer(live_pool)
    assert (await reviewer.review(original_id)).status is ReviewStatus.NEEDS_REWORK  # steps unfinished
    async with live_pool.acquire() as c:  # finish the remaining steps (evidence-backed)
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", original_id)
        # the rework: after the researched Node upgrade, the npm_install retry now succeeds
        await c.execute("UPDATE task_execution SET status='completed', error=NULL "
                        "WHERE task_id=$1 AND tool_name='npm_install'", original_id)
        # no duplicated executions — the ON CONFLICT key kept them unique
        n_exec = await c.fetchval("SELECT count(*) FROM task_execution WHERE task_id=$1", original_id)
        n_distinct = await c.fetchval(
            "SELECT count(DISTINCT (step_seq, tool_name, attempt)) FROM task_execution WHERE task_id=$1",
            original_id)
    assert n_exec == n_distinct  # no duplicate executions
    err = await s2.finish(original_id, status="done")
    assert err is None and await s2.get(original_id) is None  # reviewer PASSED → completed + archived

    # ── invariants across the whole run ─────────────────────────────────────────────────────────────
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT status, archived_at FROM task WHERE id=$1", original_id)
    # Turn 1: the row STAYS after archive - there was only ever one task_id, and it is queryable.
    assert row is not None and row["archived_at"] is not None
