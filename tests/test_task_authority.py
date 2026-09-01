"""Active Task Authority — deterministic task lifecycle enforcement.

Tests the core invariant: the user's newest request has higher authority than old memory,
retrieval, or previous task state. The system (not the LLM) decides which task is active.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.tasks.authority import TaskAction, TaskAuthority
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


# ---- TEST A: Explicit cancellation → CANCELLED (not abandoned) ----------------------------------

async def test_explicit_cancellation_is_cancelled(live_pool: Any) -> None:
    """Explicit user cancellation results in CANCELLED semantics, not ambiguous abandonment."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search", "analyze"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("stop that")

    assert transition.action == TaskAction.CANCELLED
    assert transition.previous_task is not None and transition.previous_task.id == task_a.id

    updated_a = await store.get(task_a.id)
    assert updated_a is not None
    assert updated_a.status == "cancelled"  # NOT 'abandoned'
    assert updated_a.is_primary is False

    active = await authority.current_active()
    assert active is None


async def test_cancel_forget_old_task(live_pool: Any) -> None:
    """'forget the old task' triggers cancellation with 'cancelled' status."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("forget the old task")
    assert transition.action == TaskAction.CANCELLED

    updated_a = await store.get(task_a.id)
    assert updated_a is not None and updated_a.status == "cancelled"


async def test_cancel_drop_previous(live_pool: Any) -> None:
    """'drop the previous task' triggers cancellation."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("drop the previous task")
    assert transition.action == TaskAction.CANCELLED


async def test_cancel_preserves_step_progress(live_pool: Any) -> None:
    """Cancelling a task stops execution but preserves step progress."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search", "analyze"])
    await store.advance(task_a.id, 1, "done", note="found 5 CVEs")
    await authority.activate_task(task_a.id)

    await authority.handle_new_turn("stop that")

    updated_a = await store.get(task_a.id)
    assert updated_a is not None
    assert updated_a.status == "cancelled"
    assert updated_a.steps[0].status == "done"  # step progress preserved


# ---- TEST B: Superseded task preserves execution history ----------------------------------------

async def test_superseded_waiting_task_preserves_status(live_pool: Any) -> None:
    """A waiting task superseded by another task preserves its execution status."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    # Task A is waiting on dependencies
    task_a = await store.create("Pipeline", [
        "step one",
        {"description": "step two", "depends_on": [1]},
    ])
    await authority.activate_task(task_a.id)
    await store.advance(task_a.id, 1, "done")

    # Task A is now waiting (step 2 blocked on step 1 — but step 1 is done, so it's running)
    # Let's make it explicitly waiting by using the store
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET status = 'waiting' WHERE id = $1", task_a.id)

    updated_a = await store.get(task_a.id)
    assert updated_a is not None and updated_a.status == "waiting"

    # Task B supersedes
    task_b = await store.create("Fix Laravel", ["debug"])
    await authority.activate_task(task_b.id)

    # Task A should preserve its 'waiting' status, NOT be overwritten to 'superseded'
    updated_a = await store.get(task_a.id)
    assert updated_a is not None
    assert updated_a.status == "waiting"  # execution state preserved
    assert updated_a.is_primary is False  # authority removed
    assert updated_a.superseded_by == task_b.id  # authority tracked


async def test_superseded_paused_task_preserves_status(live_pool: Any) -> None:
    """A paused task superseded by another task preserves its 'paused' status."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research", ["search"])
    await authority.activate_task(task_a.id)

    # Explicitly set to paused
    async with live_pool.acquire() as conn:
        await conn.execute("UPDATE task SET status = 'paused' WHERE id = $1", task_a.id)

    task_b = await store.create("Fix Laravel", ["debug"])
    await authority.activate_task(task_b.id)

    updated_a = await store.get(task_a.id)
    assert updated_a is not None
    assert updated_a.status == "paused"  # preserved
    assert updated_a.is_primary is False
    assert updated_a.superseded_by == task_b.id


# ---- TEST C: "Help me understand" does NOT supersede current task --------------------------------

async def test_help_me_understand_does_not_supersede(live_pool: Any) -> None:
    """'Help me understand why the middleware is failing' should NOT supersede
    'Fix Laravel authentication' — it's a refinement/continuation."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Fix Laravel authentication", ["debug middleware", "fix auth"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn(
        "Help me understand why the middleware is failing")

    assert transition.action == TaskAction.CONTINUED
    active = await authority.current_active()
    assert active is not None and active.id == task_a.id


async def test_why_does_not_supersede(live_pool: Any) -> None:
    """'Why is the auth failing?' is a question about the current task."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Fix Laravel authentication", ["debug"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("Why is the auth failing?")
    assert transition.action == TaskAction.CONTINUED


async def test_how_do_does_not_supersede(live_pool: Any) -> None:
    """'How do I configure the middleware?' is a refinement question."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Fix Laravel authentication", ["debug"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("How do I configure the middleware?")
    assert transition.action == TaskAction.CONTINUED


# ---- TEST D: "Research API attacks more deeply" does NOT supersede --------------------------------

async def test_research_more_deeply_does_not_supersede(live_pool: Any) -> None:
    """'Research API attacks more deeply' should NOT supersede 'Research web attacks'
    — it's a refinement of the same topic."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search", "analyze"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("Research API attacks more deeply")

    assert transition.action == TaskAction.CONTINUED
    active = await authority.current_active()
    assert active is not None and active.id == task_a.id


async def test_also_does_not_supersede(live_pool: Any) -> None:
    """'Also check the database layer' is a continuation."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Fix Laravel authentication", ["debug"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("Also check the database layer")
    assert transition.action == TaskAction.CONTINUED


# ---- TEST E: Explicit replacement DOES supersede -------------------------------------------------

async def test_forget_that_now_help_me_fix(live_pool: Any) -> None:
    """'Forget that. Now help me fix Laravel' — cancellation fires first (explicit stop)."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    # "Forget that" matches cancellation — this is correct: the user explicitly stopped.
    transition = await authority.handle_new_turn("Forget that, now help me fix Laravel")

    assert transition.action == TaskAction.CANCELLED
    updated_a = await store.get(task_a.id)
    assert updated_a is not None and updated_a.status == "cancelled"


async def test_instead_supersedes(live_pool: Any) -> None:
    """'Instead, help me with the database' explicitly replaces."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("Instead, help me with the database")
    assert transition.action == TaskAction.SUPERSEDED


async def test_new_task_supersedes(live_pool: Any) -> None:
    """'New task: fix the deploy script' explicitly replaces."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("New task: fix the deploy script")
    assert transition.action == TaskAction.SUPERSEDED


async def test_change_topic_supersedes(live_pool: Any) -> None:
    """'Change topic — let's talk about Docker' explicitly replaces."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("Change topic, let's talk about Docker")
    assert transition.action == TaskAction.SUPERSEDED


# ---- TEST F: Concurrent activation cannot leave two primary tasks --------------------------------

async def test_concurrent_activation_single_primary(live_pool: Any) -> None:
    """Rapid activation of multiple tasks leaves exactly one primary."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    tasks = []
    for i in range(5):
        t = await store.create(f"Task {i}", [f"step {i}"])
        tasks.append(t)

    # Activate all rapidly
    for t in tasks:
        await authority.activate_task(t.id)

    # Only the last one should be primary
    active = await authority.current_active()
    assert active is not None and active.id == tasks[-1].id

    # Verify only one has is_primary=true
    async with live_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM task WHERE is_primary")
        assert count == 1


# ---- TEST G: Terminal task cannot remain authoritative primary ------------------------------------

async def test_done_task_cannot_be_active(live_pool: Any) -> None:
    """A done task cannot be the active primary task."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research", ["search"])
    await authority.activate_task(task_a.id)
    await store.advance(task_a.id, 1, "done")

    # Task is done — should not be active
    active = await authority.current_active()
    assert active is None


async def test_cancelled_task_cannot_be_active(live_pool: Any) -> None:
    """A cancelled task cannot be the active primary task."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research", ["search"])
    await authority.activate_task(task_a.id)
    await authority.handle_new_turn("stop that")

    active = await authority.current_active()
    assert active is None


async def test_failed_task_cannot_be_active(live_pool: Any) -> None:
    """A failed task cannot be the active primary task."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research", ["search"])
    await authority.activate_task(task_a.id)
    await store.finish(task_a.id, status="failed", result="crashed")

    active = await authority.current_active()
    assert active is None


# ---- TEST H: Stale tool result cannot hijack new task -------------------------------------------

async def test_superseded_task_data_stays_isolated(live_pool: Any) -> None:
    """Task A's data remains associated with Task A after supersession.
    Task B's authority is unaffected."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    # Task A with progress
    task_a = await store.create("Research web attacks", ["search", "analyze"])
    await store.advance(task_a.id, 1, "done", note="found CVEs")
    await authority.activate_task(task_a.id)

    # Task B supersedes
    task_b = await store.create("Fix Laravel project", ["debug", "fix"])
    await authority.activate_task(task_b.id)

    # Task A's data is still there
    updated_a = await store.get(task_a.id)
    assert updated_a is not None
    assert updated_a.steps[0].status == "done"
    assert updated_a.steps[0].note == "found CVEs"
    assert updated_a.is_primary is False
    assert updated_a.superseded_by == task_b.id

    # Task B is authoritative
    active = await authority.current_active()
    assert active is not None and active.id == task_b.id

    # A continuation of Task B doesn't change anything
    transition = await authority.handle_new_turn("continue with the Laravel fix")
    assert transition.action == TaskAction.CONTINUED
    assert transition.active_task is not None and transition.active_task.id == task_b.id


# ---- Existing tests (updated) -------------------------------------------------------------------

async def test_new_task_supersedes_old(live_pool: Any) -> None:
    """User starts Task A, then starts unrelated Task B via explicit replacement."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research new web attacks in 2026", ["search CVE databases"])
    await authority.activate_task(task_a.id)

    active = await authority.current_active()
    assert active is not None and active.id == task_a.id

    # User starts an unrelated task with explicit replacement language
    transition = await authority.handle_new_turn("Instead, help me fix my Laravel project")

    assert transition.action == TaskAction.SUPERSEDED
    assert transition.previous_task is not None and transition.previous_task.id == task_a.id

    updated_a = await store.get(task_a.id)
    assert updated_a is not None and updated_a.status == "running"  # status preserved, not cancelled
    assert updated_a.is_primary is False  # but no longer primary

    active = await authority.current_active()
    assert active is None


async def test_new_task_supersedes_via_plan(live_pool: Any) -> None:
    """When plan_task creates a new task, it activates through authority and supersedes old."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search", "analyze"])
    await authority.activate_task(task_a.id)

    task_b = await store.create("Fix Laravel project", ["debug", "fix", "test"])
    await authority.activate_task(task_b.id)

    updated_a = await store.get(task_a.id)
    assert updated_a is not None
    assert updated_a.is_primary is False
    assert updated_a.superseded_by == task_b.id

    active = await authority.current_active()
    assert active is not None and active.id == task_b.id


async def test_resume_superseded_task(live_pool: Any) -> None:
    """Task A was superseded. User says 'resume the previous task'. Task A → ACTIVE."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search", "analyze"])
    await authority.activate_task(task_a.id)

    task_b = await store.create("Fix Laravel project", ["debug"])
    await authority.activate_task(task_b.id)

    transition = await authority.handle_new_turn("resume the previous task")

    assert transition.action == TaskAction.RESUMED
    assert transition.new_task is not None and transition.new_task.id == task_a.id

    active = await authority.current_active()
    assert active is not None and active.id == task_a.id

    updated_b = await store.get(task_b.id)
    assert updated_b is not None and updated_b.is_primary is False


async def test_resume_previous_task(live_pool: Any) -> None:
    """'resume the previous task' brings back the most recently superseded task."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Task A", ["step1"])
    await authority.activate_task(task_a.id)

    task_b = await store.create("Task B", ["step1"])
    await authority.activate_task(task_b.id)

    transition = await authority.handle_new_turn("resume the previous task")
    assert transition.action == TaskAction.RESUMED
    assert transition.new_task is not None and transition.new_task.id == task_a.id


async def test_memory_cannot_reactivate_task(live_pool: Any) -> None:
    """Memory retrieval cannot reactivate an old task. Task B stays active."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search"])
    await authority.activate_task(task_a.id)

    task_b = await store.create("Fix Laravel project", ["debug"])
    await authority.activate_task(task_b.id)

    active = await authority.current_active()
    assert active is not None and active.id == task_b.id

    transition = await authority.handle_new_turn("continue with the Laravel fix")
    assert transition.action == TaskAction.CONTINUED
    assert transition.active_task is not None and transition.active_task.id == task_b.id


async def test_no_active_task_returns_none(live_pool: Any) -> None:
    """When there's no active task, handle_new_turn returns NONE action."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    transition = await authority.handle_new_turn("hello")
    assert transition.action == TaskAction.NONE
    assert transition.active_task is None


async def test_context_block_is_deterministic(live_pool: Any) -> None:
    """The context block contains task ID, status, and objective — not inferred by LLM."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Research web attacks", ["search CVE databases", "analyze trends"])
    await authority.activate_task(task_a.id)

    transition = await authority.handle_new_turn("continue")
    block = transition.context_block()

    assert block is not None
    assert "CURRENT PRIMARY TASK" in block
    assert "Research web attacks" in block
    assert "RUNNING" in block
    assert str(task_a.id) in block
    assert "CURRENT OBJECTIVE" in block


async def test_only_one_primary_at_a_time(live_pool: Any) -> None:
    """The unique index enforces at most one is_primary=true task."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Task A", ["step1"])
    task_b = await store.create("Task B", ["step1"])

    await authority.activate_task(task_a.id)
    active = await authority.current_active()
    assert active is not None and active.id == task_a.id

    await authority.activate_task(task_b.id)
    active = await authority.current_active()
    assert active is not None and active.id == task_b.id

    updated_a = await store.get(task_a.id)
    assert updated_a is not None and updated_a.is_primary is False


async def test_cancellation_patterns(live_pool: Any) -> None:
    """Various cancellation phrases are all detected."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    patterns = [
        "stop that",
        "cancel that",
        "forget the old task",
        "forget that task",
        "don't continue that",
        "drop the previous task",
        "leave that task",
        "abort that",
        "never mind that task",
        "quit that task",
        "halt that task",
    ]

    for pattern in patterns:
        task = await store.create(f"Task for {pattern}", ["step1"])
        await authority.activate_task(task.id)

        transition = await authority.handle_new_turn(pattern)
        assert transition.action == TaskAction.CANCELLED, f"Pattern not detected: {pattern!r}"

        updated = await store.get(task.id)
        assert updated is not None
        assert updated.status == "cancelled", (
            f"Status should be 'cancelled' for pattern: {pattern!r}, got: {updated.status!r}")


async def test_resume_patterns(live_pool: Any) -> None:
    """Various resumption phrases are all detected."""
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    task_a = await store.create("Old task", ["step1"])
    await authority.activate_task(task_a.id)
    task_b = await store.create("New task", ["step1"])
    await authority.activate_task(task_b.id)

    patterns = [
        "resume the previous task",
        "continue the old task",
        "go back to the old task",
        "pick up where we left off",
        "return to the previous task",
    ]

    for pattern in patterns:
        await authority.activate_task(task_b.id)

        transition = await authority.handle_new_turn(pattern)
        assert transition.action == TaskAction.RESUMED, f"Pattern not detected: {pattern!r}"
