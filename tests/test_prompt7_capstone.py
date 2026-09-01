"""Prompt 7 capstone (§39) — the whole adaptive-intelligence loop, end to end.

A first Laravel task is worked, researched, verified and completed; a reviewer PASS turns its lesson
into evidence-backed knowledge; the daily cycle promotes it to durable, reusable memory; the process
restarts; a SECOND Laravel task — a different task with the same stable Sali identity — retrieves that
learned knowledge and benefits from it, while still being gated by the reviewer and never inheriting the
first task's workspace or state. This proves: experience → evidence → learning → future intelligence,
with one Sali, one durable identity, and no fabricated learning.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.events.publisher import EventPublisher
from sali.learning.candidates import LearningCandidateStore
from sali.learning.daily import DailyConsolidation
from sali.learning.retrieval import relevant_knowledge
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))
    pub = EventPublisher(pool)
    for st in (loop._tasks, loop._research_store, loop._decisions, loop._phases):
        st._publisher = pub
    loop._reviewer = TaskReviewer(pool, pub)
    loop._tasks._reviewer = loop._reviewer
    return loop


async def _complete_all_steps(pool: Any, task_id: Any) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def test_prompt7_capstone(live_pool: Any) -> None:
    LESSON = "Laravel Breeze installs authentication scaffolding via composer require laravel/breeze --dev"

    # ── TASK 1: work it, research, record a lesson, pass review, complete ────────────────────────────
    loop1 = _loop(live_pool, FakeModelProvider())
    store = loop1._tasks
    t1 = await store.create("Build a Laravel app with authentication", ["scaffold", "auth"])
    await store.activate(t1.id)
    task1_id = t1.id
    bound = await store.bind_workspace(t1.id, objective="Build a Laravel app", explicit=None,
                                       sali_works_root="/home/almir/Desktop/sali-works")
    task1_workspace = bound["workspace_root"]
    # a research finding that led to the solution, then the lesson learned from it
    rid = await loop1._research_store.record_research(
        task_id=t1.id, run_id=uuid4(), step_seq=2, query="Laravel auth scaffolding",
        source="https://laravel.com/docs/breeze", summary="composer require laravel/breeze --dev",
        confidence=0.8, content_hash="h1")
    cand_id = await loop1._research_store.record_candidate(
        task_id=t1.id, run_id=uuid4(), lesson=LESSON, source="https://laravel.com/docs/breeze",
        research_id=rid)
    # before the reviewer passes, the lesson is NOT promotable — it is only an unverified candidate (§20)
    assert not await LearningCandidateStore(live_pool).promotable()

    # complete every step + finish with the reviewer gate → PASS turns the candidate into evidence (§21)
    await _complete_all_steps(live_pool, t1.id)
    err = await store.finish(t1.id, status="done")
    assert err is None
    async with live_pool.acquire() as c:  # the task is gone; the lesson survives (task_id → NULL)
        assert await c.fetchval("SELECT count(*) FROM task WHERE id=$1", task1_id) == 0
        cand = await c.fetchrow("SELECT verification_state, evidence_level, task_id FROM "
                                "learning_candidate WHERE id=$1", cand_id)
    assert cand["verification_state"] == "verified" and cand["evidence_level"] == 5
    assert cand["task_id"] is None   # outlived its task, still evidence-backed

    # ── DAILY CONSOLIDATION: promote the verified lesson into durable, reusable knowledge (§9/§21) ────
    summary = await DailyConsolidation(live_pool, publisher=EventPublisher(live_pool)).run(
        on_date=date(2026, 8, 31))
    assert summary.promoted >= 1
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT promoted FROM learning_candidate WHERE id=$1", cand_id) is True
        assert await c.fetchval("SELECT 1 FROM memory WHERE claim_key=$1 AND valid_until IS NULL",
                                f"lesson:{cand_id}")

    # ── PROCESS RESTART: brand-new loop/stores; the knowledge is reconstructed from PostgreSQL ───────
    loop2 = _loop(live_pool, FakeModelProvider())

    # ── TASK 2: a DIFFERENT task, SAME Sali; it retrieves the learned knowledge and benefits ─────────
    t2 = await loop2._tasks.create("Build another Laravel project with login", ["scaffold", "auth"])
    await loop2._tasks.activate(t2.id)
    bound2 = await loop2._tasks.bind_workspace(t2.id, objective="Build another Laravel project",
                                               explicit=None, sali_works_root="/home/almir/Desktop/sali-works")
    assert t2.id != task1_id                                   # a different task_id …
    assert bound2["workspace_root"] != task1_workspace         # … its own workspace, not task 1's
    # adaptive planning: the second task retrieves the first task's verified lesson (§20)
    hits = await relevant_knowledge(live_pool, objective="Build another Laravel project with login",
                                    scope_ref=bound2["workspace_root"])
    lessons = [h["lesson"] for h in hits]
    assert LESSON in lessons                                   # benefits from previous experience
    # NOT fabricated — the retrieved knowledge traces to the real, verified, promoted candidate
    assert any(h["id"] == cand_id for h in hits)

    # it does NOT inherit the first task's identity/state, and the reviewer still gates it (§39)
    assert (await loop2._tasks.get(task1_id)) is None          # task 1's state is not carried over
    assert (await loop2._reviewer.review(t2.id)).status is ReviewStatus.NEEDS_REWORK  # reviewer not bypassed

    # finish task 2 properly → the reviewer verifies the real result (completion stays evidence-based)
    await _complete_all_steps(live_pool, t2.id)
    assert (await loop2._reviewer.review(t2.id)).status is ReviewStatus.PASSED
    assert await loop2._tasks.finish(t2.id, status="done") is None
