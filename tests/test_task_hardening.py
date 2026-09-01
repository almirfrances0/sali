"""Task execution hardening tests.

Tests for completion verification, step validation, dependency enforcement,
heartbeat during tools, and crash recovery improvements.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


# ── 1. Step cannot become done without required verification ──────────────────

async def test_step_rejects_done_without_verification_when_executions_exist(
    live_pool: Any,
) -> None:
    """A step with tool executions requires verified_by to mark done."""
    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS"])
    await store.activate(task.id)

    # Record AND complete a tool execution for step 1
    exec_id = await store.record_execution(
        task.id, 1, "create_file", tool_args={"path": "/tmp/test.html"})
    await store.complete_execution(exec_id, status="completed")

    # Try to mark step 1 done WITHOUT verified_by — should fail
    result, err = await store.advance(task.id, 1, "done")
    assert result is None
    assert err is not None
    assert "no verified evidence" in err

    # Step should still be pending
    got = await store.get(task.id)
    assert got is not None
    assert got.steps[0].status == "pending"


# ── 2. Valid verified execution allows step completion ────────────────────────

async def test_step_accepts_done_with_verification(live_pool: Any) -> None:
    """A step with verified_by succeeds."""
    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS"])
    await store.activate(task.id)

    # Record and complete a tool execution
    exec_id = await store.record_execution(
        task.id, 1, "create_file", tool_args={"path": "/tmp/test.html"})
    await store.complete_execution(exec_id, status="completed")

    # Mark step 1 done WITH verified_by — should succeed
    result, err = await store.advance(task.id, 1, "done", verified_by=exec_id)
    assert err is None
    assert result is not None
    assert result.steps[0].status == "done"
    assert result.steps[0].verified is True


# ── 3. Non-tool step can complete legitimately ───────────────────────────────

async def test_step_without_executions_accepts_done(live_pool: Any) -> None:
    """A step with no tool executions (planning/observation) can be marked done."""
    store = TaskStore(live_pool)
    task = await store.create("Research", ["analyze requirements", "write spec"])
    await store.activate(task.id)

    # No tool executions recorded — step is purely descriptive
    result, err = await store.advance(task.id, 1, "done", note="analyzed requirements")
    assert err is None
    assert result is not None
    assert result.steps[0].status == "done"


# ── 4. finish_task rejects unfinished required steps ─────────────────────────

async def test_finish_rejects_done_with_incomplete_steps(live_pool: Any) -> None:
    """finish_task(status='done') rejects if steps are incomplete."""
    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS", "add JS"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    err = await store.finish(task.id, status="done", result="all done")
    assert err is not None
    assert "incomplete" in err

    # Task should still be running
    got = await store.get(task.id)
    assert got is not None
    assert got.status == "running"


# ── 5. finish_task succeeds when all steps complete ──────────────────────────

async def test_finish_succeeds_when_all_steps_done(live_pool: Any) -> None:
    """finish_task(status='done') succeeds when all steps are done."""
    store = TaskStore(live_pool)
    task = await store.create("Quick task", ["do thing"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    err = await store.finish(task.id, status="done", result="done")
    assert err is None


# ── 6. finish_task allows abandon/failed regardless of steps ─────────────────

async def test_finish_allows_abandon_regardless_of_steps(live_pool: Any) -> None:
    """finish_task(status='abandoned') works even with incomplete steps."""
    store = TaskStore(live_pool)
    task = await store.create("Abandoned task", ["step1", "step2"])
    await store.activate(task.id)

    err = await store.finish(task.id, status="abandoned", result="not worth it")
    assert err is None


# ── 7. Invalid step_seq is rejected ──────────────────────────────────────────

async def test_invalid_step_seq_rejected(live_pool: Any) -> None:
    """advance() with non-existent step_seq returns error, no state change."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1", "step2"])
    await store.activate(task.id)

    # Try to advance step 99 (doesn't exist)
    result, err = await store.advance(task.id, 99, "done")
    assert result is None
    assert err is not None
    assert "does not exist" in err

    # Task should be unchanged
    got = await store.get(task.id)
    assert got is not None
    assert all(s.status == "pending" for s in got.steps)


# ── 8. Negative step_seq is rejected ─────────────────────────────────────────

async def test_negative_step_seq_rejected(live_pool: Any) -> None:
    """advance() with negative step_seq returns error."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)

    result, err = await store.advance(task.id, -1, "done")
    assert result is None
    assert err is not None


# ── 9. Dependency violation is rejected ──────────────────────────────────────

async def test_dependency_violation_rejected(live_pool: Any) -> None:
    """Cannot mark step done if its dependencies are not satisfied."""
    store = TaskStore(live_pool)
    task = await store.create("Pipeline", [
        "step one",
        {"description": "step two", "depends_on": [1]},
        {"description": "step three", "depends_on": [1, 2]},
    ])
    await store.activate(task.id)

    # Try to mark step 3 done before steps 1 and 2
    result, err = await store.advance(task.id, 3, "done")
    assert result is None
    assert err is not None
    assert "dependencies not satisfied" in err

    # Step 1 done — step 3 still blocked
    await store.advance(task.id, 1, "done")
    result, err = await store.advance(task.id, 3, "done")
    assert result is None
    assert err is not None

    # Step 2 done — step 3 now allowed (auto-completes and archives)
    await store.advance(task.id, 2, "done")
    result, err = await store.advance(task.id, 3, "done")
    assert err is None
    # Task auto-archived — result is None
    assert result is None


# ── 10. Valid independent step still works ───────────────────────────────────

async def test_independent_step_works(live_pool: Any) -> None:
    """Steps without dependencies can be completed in any order."""
    store = TaskStore(live_pool)
    task = await store.create("Independent", ["step A", "step B", "step C"])
    await store.activate(task.id)

    # Complete step 3 first (no dependencies)
    result, err = await store.advance(task.id, 3, "done")
    assert err is None
    assert result is not None
    assert result.steps[2].status == "done"


# ── 11. Heartbeat updates during task execution ──────────────────────────────

async def test_heartbeat_updates(live_pool: Any) -> None:
    """Heartbeat timestamp updates on a running task."""
    store = TaskStore(live_pool)
    task = await store.create("Heartbeat test", ["step1"])
    await store.activate(task.id)

    await store.heartbeat(task.id)
    got = await store.get(task.id)
    assert got is not None
    assert got.last_heartbeat is not None


# ── 12. Recovery detects orphaned executions ─────────────────────────────────

async def test_recovery_detects_orphaned_executions(live_pool: Any) -> None:
    """Recovery identifies steps with successful executions that weren't advanced."""
    from sali.tasks.recovery import mark_task_interrupted, recover_task

    store = TaskStore(live_pool)
    task = await store.create("Crash test", ["compile", "test", "deploy"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    # Simulate: step 2 had a successful tool execution but wasn't advanced
    exec_id = await store.record_execution(
        task.id, 2, "execute_command", tool_args={"command": "npm test"})
    await store.complete_execution(exec_id, status="completed")

    # Mark as interrupted (simulating crash)
    await mark_task_interrupted(live_pool, task.id, reason="test_crash")

    # Recover
    recovery = await recover_task(live_pool, task.id)
    assert len(recovery["orphaned_executions"]) == 1
    assert recovery["orphaned_executions"][0]["step_seq"] == 2
    assert recovery["orphaned_executions"][0]["tool_name"] == "execute_command"


# ── 13. Recovery does not rerun already-successful execution ─────────────────

async def test_recovery_preserves_completed_steps(live_pool: Any) -> None:
    """Completed steps stay completed after recovery."""
    from sali.tasks.recovery import mark_task_interrupted, recover_task

    store = TaskStore(live_pool)
    task = await store.create("Recovery test", ["step1", "step2", "step3"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")
    await store.advance(task.id, 2, "done")

    await mark_task_interrupted(live_pool, task.id, reason="crash")
    recovery = await recover_task(live_pool, task.id)

    assert recovery["completed_steps"] == 2
    assert recovery["total_steps"] == 3


# ── 14. Task completion is transactionally safe ──────────────────────────────

async def test_advance_is_atomic(live_pool: Any) -> None:
    """advance() is atomic — validation failure causes no state mutation."""
    store = TaskStore(live_pool)
    task = await store.create("Atomic test", ["step1", "step2"])
    await store.activate(task.id)

    # Try invalid step — should not change anything
    result, err = await store.advance(task.id, 99, "done")
    assert result is None and err is not None

    # Try dependency violation — should not change anything
    await store.advance(task.id, 1, "done")
    # step 2 has no dependency on step 1 in this task, so it should work
    result, err = await store.advance(task.id, 2, "done")
    assert err is None


# ── 15. Build recovery context block includes orphaned executions ─────────────

async def test_recovery_context_includes_orphaned(live_pool: Any) -> None:
    """Recovery context block mentions orphaned executions."""
    from sali.tasks.recovery import (
        build_recovery_context_block,
        mark_task_interrupted,
        recover_task,
    )

    store = TaskStore(live_pool)
    task = await store.create("Build site", ["compile", "test"])
    await store.activate(task.id)

    # Step 1 has successful execution but wasn't advanced
    exec_id = await store.record_execution(
        task.id, 1, "execute_command", tool_args={"command": "make"})
    await store.complete_execution(exec_id, status="completed")

    await mark_task_interrupted(live_pool, task.id, reason="crash")
    recovery = await recover_task(live_pool, task.id)
    block = build_recovery_context_block(recovery)

    assert "ORPHANED EXECUTIONS" in block
    assert "execute_command" in block
    assert "advance_task" in block
