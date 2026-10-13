"""Just-in-time research + evidence-aware learning (Prompt 5 §14-22). Research is durable, task-linked
evidence — never temporary context, never automatic permanent truth, and never reviewer PASS proof."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.tasks.research import ResearchStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


async def _task(pool: Any, steps: list[str]) -> Any:
    store = TaskStore(pool)
    t = await store.create("Build a Laravel app", steps)
    await store.activate(t.id)
    return t


# ── research is durable, task-linked evidence (§16/§17/§21) ──────────────────────────────────────────

async def test_research_recorded_and_listed(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    rs = ResearchStore(live_pool, pub)
    t = await _task(live_pool, ["a"])
    rid = await rs.record_research(
        task_id=t.id, run_id=uuid4(), step_seq=1, query="Laravel 13 install command",
        source="https://laravel.com/docs", summary="composer create-project laravel/laravel .",
        confidence=0.7, content_hash="abc")
    found = await rs.list_research(t.id)
    assert len(found) == 1 and found[0]["id"] == rid and found[0]["query"].startswith("Laravel")
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='research.completed' AND subject_id=$1", t.id)
    assert n >= 1


async def test_research_persists_across_restart(live_pool: Any) -> None:
    t = await _task(live_pool, ["a"])
    await ResearchStore(live_pool).record_research(
        task_id=t.id, run_id=None, step_seq=None, query="q", source="s", summary="the answer",
        confidence=0.5, content_hash="h")
    fresh = ResearchStore(live_pool)  # a new store, as after a restart, re-reads durable state
    assert (await fresh.list_research(t.id))[0]["summary"] == "the answer"


async def test_research_failure_does_not_destroy_task(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    rs = ResearchStore(live_pool, pub)
    t = await _task(live_pool, ["a"])
    await rs.record_research(
        task_id=t.id, run_id=None, step_seq=None, query="blocked query", source=None,
        summary="(no results)", confidence=0.0, content_hash=None, status="failed")
    # the task is untouched — still there, still running
    still = await TaskStore(live_pool).get(t.id)
    assert still is not None and still.status == "running"
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='research.failed' AND subject_id=$1", t.id)
    assert n >= 1


# ── research is NOT reviewer completion evidence (§22) ────────────────────────────────────────────────

async def test_research_does_not_count_as_reviewer_pass(live_pool: Any) -> None:
    rs = ResearchStore(live_pool)
    reviewer = TaskReviewer(live_pool)
    t = await _task(live_pool, ["a", "b"])
    # lots of research, but a step is still pending → the reviewer must NOT pass on research alone
    async with live_pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1 AND seq=1", t.id)
    await rs.record_research(
        task_id=t.id, run_id=None, step_seq=2, query="how to do step b",
        source="https://docs", summary="docs say run the command", confidence=0.9, content_hash="h")
    r = await reviewer.review(t.id)
    assert r.status is ReviewStatus.NEEDS_REWORK  # step b has no execution evidence — research isn't proof


# ── evidence-aware learning candidates (§18/§19/§20/§21) ─────────────────────────────────────────────

async def test_verified_research_usage_creates_promotable_candidate(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    rs = ResearchStore(live_pool, pub)
    store = TaskStore(live_pool, pub)
    store._reviewer = TaskReviewer(live_pool, pub)  # gate wired so completion verifies candidates
    t = await _task(live_pool, ["a"])
    cid = await rs.record_candidate(
        task_id=t.id, run_id=None, lesson="Laravel 13 installs with composer create-project .",
        source="https://laravel.com/docs")
    assert not await rs.promotable_candidates()  # unverified while the task is unfinished
    # completing the task (the reviewer PASSES) verifies its candidates → now promotable (§19)
    updated, err = await store.advance(t.id, 1, "done", verified_by=uuid4())
    assert err is None and updated is None  # auto-completed via the reviewer gate
    promotable = await rs.promotable_candidates()
    assert any(c["id"] == cid for c in promotable)
    async with live_pool.acquire() as c:
        created = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='learning.candidate_created'")
    assert created >= 1


async def test_failed_research_usage_cannot_become_verified_lesson(live_pool: Any) -> None:
    rs = ResearchStore(live_pool)
    t = await _task(live_pool, ["a", "b"])  # a task that will NOT be completed
    cid = await rs.record_candidate(
        task_id=t.id, run_id=None, lesson="a guess that was never verified")
    # the task never passes review → its candidate stays unverified → never promotable (§20/§21)
    cands = await rs.list_candidates(task_id=t.id)
    assert len(cands) == 1 and cands[0]["id"] == cid and cands[0]["verification_state"] == "unverified"
    assert not await rs.promotable_candidates()


async def test_mark_used_flips_used_and_emits(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    rs = ResearchStore(live_pool, pub)
    t = await _task(live_pool, ["a"])
    rid = await rs.record_research(
        task_id=t.id, run_id=None, step_seq=None, query="q", source="s", summary="x",
        confidence=0.5, content_hash="h")
    await rs.mark_used(rid, t.id)
    assert (await rs.list_research(t.id))[0]["used"] is True
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='research.used' AND subject_id=$1", t.id)
    assert n >= 1
