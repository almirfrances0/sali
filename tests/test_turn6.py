"""Turn 6 of the Work-audit: MODIFY_PRIMARY_TASK category + ModifyTask extensions.

Closes the "mid-task correction silently mapped to CONTINUE_PRIMARY" gap:
  1. attention.classify recognizes correction phrases when a primary is active.
  2. runtime.handle_message preempts + resumes for MODIFY_PRIMARY_TASK (like CONVERSATION).
  3. ModifyTask.edit_step / change_objective work non-destructively.
  4. task.modified event lands on the bus for iOS + WS replay.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.runtime.attention import AttentionCategory, classify
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


# ── (1) attention classify: correction phrases become MODIFY_PRIMARY_TASK ────────

@pytest.mark.parametrize("phrase", [
    # Every alternative anchored on an explicit step, plan, task, or objective reference
    # (Turn 6 hardening: broad phrases like "actually use tabs" or "add another column" are
    # now correctly refused - they could just as easily be new-task requests).
    "change step 3",
    "edit step 2 to use redis",
    "add a step to run the tests",
    "add another step",
    "insert one more step",
    "skip step 3",
    "remove step 5",
    "redo step 2",
    "reset step 4",
    "rename this task to Q3 rollout",
    "retitle this task",
    "on step 4 use gzip",
    "for step 2 use JSON",
    "in step 1 skip validation",
    "update the plan to include migrations",
    "change the objective",
    "actually use step",
    "actually, skip step 4",
    "forgot to add a step",
])
def test_correction_phrase_with_primary_matches_modify(phrase: str) -> None:
    """Correction-shaped phrases with a primary active route to MODIFY_PRIMARY_TASK."""
    decision = classify(phrase, has_primary=True, current_objective="build a website")
    assert decision.category == AttentionCategory.MODIFY_PRIMARY_TASK, (
        f"{phrase!r} should be MODIFY, got {decision.category.value} ({decision.reason})")


@pytest.mark.parametrize("phrase", [
    # These matched the OLD too-broad regex and would silently misroute a fresh
    # request onto the running task. Under the tightened regex they now correctly
    # do NOT route as MODIFY (they'll route as REPLACE/CONTINUE/etc based on other rules).
    "actually use tabs not spaces",       # a preference, not a step edit
    "actually make it use redis",         # ambiguous new-request
    "change it to use PostgreSQL",         # bare pronoun, no plan anchor
    "add another column too",              # unrelated to the running task
    "include a section on migrations",     # new-task announcement
    "also add error handling",             # not anchored to any step
    "also update the header",              # ambiguous
    "use tabs instead of spaces",          # a preference
    "forgot to add authentication",        # new-task announcement
])
def test_previously_false_positive_phrases_no_longer_match_modify(phrase: str) -> None:
    """Turn 6 hardening: 9 phrases the initial regex misrouted are now correctly NOT MODIFY."""
    decision = classify(phrase, has_primary=True, current_objective="build a website")
    assert decision.category != AttentionCategory.MODIFY_PRIMARY_TASK, (
        f"{phrase!r} should no longer route as MODIFY (it was a false positive); "
        f"got {decision.category.value}")


def test_correction_phrase_without_primary_falls_to_continue() -> None:
    """Same phrase with NO primary → falls to the "new request becomes the focus" branch,
    not MODIFY (which only makes sense when there's something to modify)."""
    decision = classify("add a step to run the tests", has_primary=False)
    assert decision.category != AttentionCategory.MODIFY_PRIMARY_TASK


# ── (2) ordering: cancel/queue still win over modify ─────────────────────────────

def test_bare_stop_still_wins_over_modify() -> None:
    decision = classify("stop", has_primary=True, current_objective="build a website")
    assert decision.category == AttentionCategory.CANCEL_PRIMARY_TASK


def test_queue_still_wins_over_modify() -> None:
    """`after you finish, also add error handling` is queue-for-later, not modify."""
    decision = classify("after you finish, also add error handling",
                        has_primary=True, current_objective="build a website")
    assert decision.category == AttentionCategory.QUEUE_FOR_LATER


def test_replace_still_wins_over_modify() -> None:
    """`do this instead` is explicit replacement; must NOT be routed as modify."""
    decision = classify("Instead, help me build a Django REST API",
                        has_primary=True, current_objective="build a website")
    assert decision.category == AttentionCategory.REPLACE_PRIMARY_TASK


# ── (3) touches_primary now includes MODIFY ─────────────────────────────────────

def test_modify_touches_primary() -> None:
    from sali.runtime.attention import AttentionDecision, Priority
    d = AttentionDecision(AttentionCategory.MODIFY_PRIMARY_TASK, Priority.NORMAL, "x")
    assert d.touches_primary is True

    d2 = AttentionDecision(AttentionCategory.CONTINUE_PRIMARY, Priority.NORMAL, "x")
    assert d2.touches_primary is False


# ── (4) ModifyTask edit_step: non-destructive description edit ───────────────────

async def test_edit_step_preserves_execution_state(live_pool: Any) -> None:
    """edit_step changes the description but leaves status/attempts/checkpoint alone —
    unlike 'replace' which wipes them."""
    from sali.config.settings import Settings
    from sali.core.clock import SystemClock
    from sali.tools.builtins.task_tool import ModifyTask
    from sali.tools.context import ToolContext

    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("test task", ["run first thing", "run second thing"])
    await store.activate(task.id)

    # Simulate real progress on step 1: mark it running with 1 attempt (as if a tool ran).
    async with live_pool.acquire() as c:
        await c.execute(
            "UPDATE sali.task_step SET status='running', attempts=1, last_error='transient' "
            "WHERE task_id=$1 AND seq=1", task.id)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool,
                      run_id=uuid4(), tasks=store)
    res = await ModifyTask().run(
        {"action": "edit_step", "step": 1, "description": "run the FIRST step (renamed)"}, ctx)
    assert res.ok, f"edit_step failed: {res.error}"

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT description, status, attempts, last_error FROM sali.task_step "
            "WHERE task_id=$1 AND seq=1", task.id)
    assert row["description"] == "run the FIRST step (renamed)"
    # THE CRITICAL INVARIANT: execution state MUST survive an edit_step.
    assert row["status"] == "running", "edit_step must not reset status"
    assert row["attempts"] == 1, "edit_step must not reset attempts"
    assert row["last_error"] == "transient", "edit_step must not clear last_error"


# ── (5) ModifyTask change_objective ──────────────────────────────────────────────

async def test_change_objective_renames_task(live_pool: Any) -> None:
    from sali.config.settings import Settings
    from sali.core.clock import SystemClock
    from sali.tools.builtins.task_tool import ModifyTask
    from sali.tools.context import ToolContext

    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Old objective", ["step 1"])
    await store.activate(task.id)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool,
                      run_id=uuid4(), tasks=store)
    res = await ModifyTask().run(
        {"action": "change_objective", "description": "New objective for Q3"}, ctx)
    assert res.ok, f"change_objective failed: {res.error}"

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT objective, status, is_primary FROM sali.task WHERE id=$1", task.id)
    assert row["objective"] == "New objective for Q3"
    assert row["status"] in ("open", "running")      # unchanged
    assert row["is_primary"] is True    # unchanged


# ── (6) task.modified event lands on the bus for every ModifyTask action ────────

async def test_edit_step_emits_task_modified_event(live_pool: Any) -> None:
    """Every ModifyTask mutation now publishes task.modified — iOS + WS replay see it."""
    from sali.config.settings import Settings
    from sali.core.clock import SystemClock
    from sali.tools.builtins.task_tool import ModifyTask
    from sali.tools.context import ToolContext

    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("A task", ["step 1"])
    await store.activate(task.id)

    async with live_pool.acquire() as c:
        seq_before = await c.fetchval(
            "SELECT coalesce(max(seq), 0) FROM sali.event WHERE subject_id=$1", task.id)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool,
                      run_id=uuid4(), tasks=store)
    await ModifyTask().run(
        {"action": "edit_step", "step": 1, "description": "step one renamed"}, ctx)

    async with live_pool.acquire() as c:
        rows = await c.fetch(
            "SELECT event_type, payload FROM sali.event "
            "WHERE subject_id=$1 AND seq > $2 ORDER BY seq", task.id, seq_before)
    types = [r["event_type"] for r in rows]
    assert "task.modified" in types, f"task.modified must land on the bus, got: {types}"
    modified = next(r for r in rows if r["event_type"] == "task.modified")
    assert dict(modified["payload"])["action"] == "edit_step"


async def test_change_objective_emits_task_modified(live_pool: Any) -> None:
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Old title", ["step"])
    await store.activate(task.id)

    async with live_pool.acquire() as c:
        seq_before = await c.fetchval(
            "SELECT coalesce(max(seq), 0) FROM sali.event WHERE subject_id=$1", task.id)

    await store.rename_objective(task.id, "New title")

    async with live_pool.acquire() as c:
        modified = await c.fetchrow(
            "SELECT payload FROM sali.event WHERE subject_id=$1 AND event_type='task.modified' "
            "AND seq > $2 LIMIT 1", task.id, seq_before)
    assert modified is not None
    payload = dict(modified["payload"])
    assert payload["action"] == "change_objective"
    assert payload["objective"] == "New title"
