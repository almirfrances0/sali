"""Turn 8 of the Work-audit: bounded background yield + reviewer verdicts land on the task row.

  (A) coordinator._yield_background is now bounded — a tool that swallows CancelledError
      no longer wedges chat invisibly. If the grace expires, a runtime.background_wedged
      event is published so operators/iOS see the wedge.
  (B) TaskStore._review_gate mutates the task row on every settled review verdict:
      - populates denormalized last_review_{id,status,at,attempt,summary}
      - on non-pass, flips status to 'needs_changes' or 'blocked_by_review'
      - iOS TaskState decodes both and treats them as isActive.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.tasks.reviewer import ReviewResult, ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


# ── (B.1) On PASS, denorm fields populate; task can still flip to 'done' via advance ─

async def test_review_gate_pass_populates_last_review_fields(live_pool: Any) -> None:
    """Even a passing review lands last_review_* onto the task row, so history is visible."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test pass", ["step"])
    await store.activate(task.id)

    # Wire a reviewer stub that returns PASSED.
    class _PassReviewer:
        async def review(self, tid, run_id=None):
            return ReviewResult(
                review_id=uuid4(), task_id=tid, run_id=run_id,
                status=ReviewStatus.PASSED, attempt=1,
                reviewer_type="stub", summary="looks good", requirements=[],
                failures=[], recommendations=[], passed=1, failed=0, unknown=0)
    store._reviewer = _PassReviewer()

    result = await store._review_gate(task.id, run_id=None)
    assert result is True

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT status, last_review_status, last_review_summary "
            "FROM sali.task WHERE id=$1", task.id)
    # Task status is NOT flipped by the gate itself on pass — that's the caller's job.
    # But the denorm fields ARE populated so observers can see the review landed.
    assert row["last_review_status"] == "passed"
    assert row["last_review_summary"] == "looks good"


# ── (B.2) On NEEDS_REWORK, task flips to needs_changes + denorm populated ────────

async def test_review_gate_needs_rework_flips_status(live_pool: Any) -> None:
    """The audit's core gap: NEEDS_REWORK now visibly flips task.status."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test rework", ["step"])
    await store.activate(task.id)

    class _ReworkReviewer:
        async def review(self, tid, run_id=None):
            return ReviewResult(
                review_id=uuid4(), task_id=tid, run_id=run_id,
                status=ReviewStatus.NEEDS_REWORK, attempt=2,
                reviewer_type="stub", summary="2 failed of 3",
                requirements=[], failures=[{"requirement": "test"}],
                recommendations=[{"action": "fix the failing test"}],
                passed=1, failed=2, unknown=0)
    store._reviewer = _ReworkReviewer()

    result = await store._review_gate(task.id, run_id=None)
    assert result is False

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT status, last_review_status, last_review_attempt "
            "FROM sali.task WHERE id=$1", task.id)
    assert row["status"] == "needs_changes", (
        f"NEEDS_REWORK must flip task.status to needs_changes; got {row['status']}")
    assert row["last_review_status"] == "needs_rework"
    assert row["last_review_attempt"] == 2


# ── (B.3) On BLOCKED, task flips to blocked_by_review ────────────────────────────

async def test_review_gate_blocked_flips_status(live_pool: Any) -> None:
    """BLOCKED (permission needed etc.) is distinct from generic 'blocked' (external)."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test blocked", ["step"])
    await store.activate(task.id)

    class _BlockedReviewer:
        async def review(self, tid, run_id=None):
            return ReviewResult(
                review_id=uuid4(), task_id=tid, run_id=run_id,
                status=ReviewStatus.BLOCKED, attempt=1,
                reviewer_type="stub", summary="needs permission",
                requirements=[], failures=[], recommendations=[],
                passed=0, failed=0, unknown=1)
    store._reviewer = _BlockedReviewer()

    result = await store._review_gate(task.id, run_id=None)
    assert result is False

    async with live_pool.acquire() as c:
        s = await c.fetchval("SELECT status FROM sali.task WHERE id=$1", task.id)
    assert s == "blocked_by_review", f"BLOCKED must flip to blocked_by_review; got {s}"


# ── (B.4) Bare-store branch (no reviewer) still populates NOTHING ──────────────

async def test_bare_store_gate_does_not_touch_last_review_fields(live_pool: Any) -> None:
    """Bare-store bypass (no reviewer wired) must not populate last_review_* — there's no verdict."""
    store = TaskStore(live_pool)   # no reviewer wired
    task = await store.create("Bare-store", ["step"])
    await store.activate(task.id)

    result = await store._review_gate(task.id, run_id=None)
    assert result is True   # bare-store still passes

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT last_review_status, last_review_id FROM sali.task WHERE id=$1",
            task.id)
    assert row["last_review_status"] is None
    assert row["last_review_id"] is None


# ── (B.5) finish()'s magic 'review_required' string still returned (contract) ─

async def test_finish_still_returns_review_required_literal(live_pool: Any) -> None:
    """The `err == 'review_required'` branch in task_tool.py:FinishTask depends on this
    literal. Turn 8 must NOT break that contract."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test finish", ["step"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:
        await c.execute("UPDATE sali.task_step SET status='done' "
                        "WHERE task_id=$1 AND seq=1", task.id)

    class _RejectReviewer:
        async def review(self, tid, run_id=None):
            return ReviewResult(
                review_id=uuid4(), task_id=tid, run_id=run_id,
                status=ReviewStatus.NEEDS_REWORK, attempt=1,
                reviewer_type="stub", summary="not ready", requirements=[],
                failures=[], recommendations=[], passed=0, failed=1, unknown=0)
    store._reviewer = _RejectReviewer()

    err = await store.finish(task.id, status="done", run_id=None)
    assert err == "review_required", (
        f"finish() MUST return the literal 'review_required' string; got {err!r}")


# ── (A) coordinator._yield_background bounded ──────────────────────────────────

@pytest.mark.asyncio
async def test_yield_background_returns_true_on_prompt_cancel() -> None:
    """A well-behaved bg task exits on CancelledError inside grace → returns True."""
    from sali.runtime.coordinator import AgentRuntimeCoordinator as Coordinator

    coord = Coordinator.__new__(Coordinator)
    coord._publisher = None

    async def _polite() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    coord._bg_task = asyncio.create_task(_polite())
    await asyncio.sleep(0.05)   # let it start
    result = await coord._yield_background("test polite cancel")
    assert result is True


@pytest.mark.asyncio
async def test_yield_background_returns_false_on_stuck_tool() -> None:
    """A tool that swallows CancelledError blows past the grace window → returns False."""
    from sali.runtime.coordinator import AgentRuntimeCoordinator as Coordinator

    coord = Coordinator.__new__(Coordinator)
    coord._publisher = None
    # Shrink grace so the test doesn't wait 5s.
    coord._YIELD_GRACE = 0.2

    # Simulate a broken tool: it swallows the first N cancels, then propagates so
    # test teardown can end it. The yield's grace fires after 0.2s regardless.
    swallows = {'count': 0}
    async def _stuck() -> None:
        while swallows['count'] < 100:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                swallows['count'] += 1
                if swallows['count'] > 5:
                    raise
                continue

    coord._bg_task = asyncio.create_task(_stuck())
    await asyncio.sleep(0.05)
    result = await coord._yield_background("test stuck tool")
    assert result is False, "wedged tool must be detected and reported"
    # Cleanup: keep cancelling until the task actually exits (past the swallow budget).
    for _ in range(10):
        if coord._bg_task.done():
            break
        coord._bg_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(coord._bg_task), 0.05)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


@pytest.mark.asyncio
async def test_yield_background_returns_true_when_no_bg_task() -> None:
    """No bg task → trivially yielded."""
    from sali.runtime.coordinator import AgentRuntimeCoordinator as Coordinator

    coord = Coordinator.__new__(Coordinator)
    coord._publisher = None
    coord._bg_task = None
    assert await coord._yield_background("no bg") is True


# ── Turn 8 hardening: tasks in rework state can progress again ───────────────

async def test_needs_changes_task_can_advance_out(live_pool: Any) -> None:
    """After a review flips a task to needs_changes, the model must be able to
    address the rework and see the task move forward again. Was: task-row UPDATE
    was gated 'status IN (open, running)' so needs_changes was a dead end."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Rework me", ["step"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:
        await c.execute("UPDATE sali.task SET status='needs_changes' WHERE id=$1", task.id)

    updated, err = await store.advance(task.id, 1, "done", verified_by=uuid4())
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT status FROM sali.task WHERE id=$1", task.id)
    # A task with all steps settled + no reviewer should auto-complete (bare-store branch).
    # The critical property: it should NOT still be stuck on 'needs_changes'.
    assert row["status"] != "needs_changes", (
        f"task stuck in needs_changes after advancing its last step; got {row['status']}")


async def test_paused_task_flips_to_needs_changes_on_rework(live_pool: Any) -> None:
    """A task in a non-terminal-but-not-open/running state (paused/waiting) MUST still
    flip when the reviewer rejects it - otherwise the rework loop is invisible for
    exactly the classes of stuck task the audit called out."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Paused-then-reviewed", ["step"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:
        await c.execute("UPDATE sali.task SET status='paused' WHERE id=$1", task.id)

    class _ReworkReviewer:
        async def review(self, tid, run_id=None):
            return ReviewResult(
                review_id=uuid4(), task_id=tid, run_id=run_id,
                status=ReviewStatus.NEEDS_REWORK, attempt=1,
                reviewer_type="stub", summary="nope", requirements=[],
                failures=[], recommendations=[], passed=0, failed=1, unknown=0)
    store._reviewer = _ReworkReviewer()

    await store._review_gate(task.id, run_id=None)
    async with live_pool.acquire() as c:
        s = await c.fetchval("SELECT status FROM sali.task WHERE id=$1", task.id)
    assert s == "needs_changes", (
        f"paused-then-rejected task must flip to needs_changes; got {s}")
