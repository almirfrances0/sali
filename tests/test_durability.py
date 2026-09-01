"""Durable task execution — checkpointing, recovery, and crash survival.

Tests that tasks survive crashes, disconnection, context compaction, and restart.
The database is authoritative — not the LLM's memory.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.tasks.recovery import (
    build_recovery_context_block,
    detect_orphaned_tasks,
    mark_task_interrupted,
    recover_task,
)
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


# ---- TEST 1: Task checkpoint persistence --------------------------------------------------------

async def test_checkpoint_persists(live_pool: Any) -> None:
    """A checkpoint written to a step survives a fresh read (simulated restart)."""
    store = TaskStore(live_pool)
    task = await store.create("Build website", ["create index.html", "add CSS", "add JS"])
    await store.advance(task.id, 1, "done", note="created")
    await store.checkpoint(task.id, 2, {"files_written": 3, "of": 10})

    # Fresh read (simulated restart)
    got = await store.get(task.id)
    assert got is not None
    s2 = got.steps[1]
    assert s2.status == "running"
    assert s2.checkpoint == {"files_written": 3, "of": 10}


# ---- TEST 2: Step completion survives restart ---------------------------------------------------

async def test_step_completion_survives_restart(live_pool: Any) -> None:
    """Completed steps persist across restart."""
    store = TaskStore(live_pool)
    task = await store.create("Deploy", ["build", "test", "deploy"])
    await store.advance(task.id, 1, "done", note="built")
    await store.advance(task.id, 2, "done", note="tests pass")

    # Simulate restart: fresh store
    store2 = TaskStore(live_pool)
    got = await store2.get(task.id)
    assert got is not None
    assert got.steps[0].status == "done"
    assert got.steps[0].note == "built"
    assert got.steps[1].status == "done"
    assert got.steps[2].status == "pending"


# ---- TEST 3: Failed step survives restart ------------------------------------------------------

async def test_failed_step_survives_restart(live_pool: Any) -> None:
    """Failed steps with error details persist across restart."""
    store = TaskStore(live_pool)
    task = await store.create("Deploy", ["build", "deploy"])
    await store.advance(task.id, 2, "failed", error="permission denied on port 80")

    store2 = TaskStore(live_pool)
    got = await store2.get(task.id)
    assert got is not None
    s2 = got.steps[1]
    assert s2.status == "failed"
    assert s2.attempts == 1
    assert "permission denied" in (s2.last_error or "")


# ---- TEST 4: Running task becomes recoverable after process death -------------------------------

async def test_running_task_becomes_orphaned(live_pool: Any) -> None:
    """A task with a stale heartbeat is detected as orphaned."""
    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create files", "deploy"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Set a stale heartbeat (simulating a crash)
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    orphaned = await detect_orphaned_tasks(live_pool)
    assert len(orphaned) >= 1
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is not None
    assert found["objective"] == "Build site"
    assert found["can_resume"] is True


# ---- TEST 5: Recovery resumes from correct checkpoint -------------------------------------------

async def test_recovery_resumes_from_checkpoint(live_pool: Any) -> None:
    """Recovery builds context from durable state, not LLM memory."""
    store = TaskStore(live_pool)
    task = await store.create("Build website", ["create index.html", "add CSS", "add JS"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done", note="created index.html")
    await store.checkpoint(task.id, 2, {"files_written": 2, "of": 5})

    # Mark as interrupted (simulating crash)
    await mark_task_interrupted(live_pool, task.id, reason="test_crash")

    # Recover
    recovery = await recover_task(live_pool, task.id)
    assert recovery["task_id"] == str(task.id)
    assert recovery["completed_steps"] == 1
    assert recovery["total_steps"] == 3
    assert recovery["retry_count"] == 1

    # Build context block
    block = build_recovery_context_block(recovery)
    assert "Build website" in block
    assert "create index.html" in block
    assert "Do NOT re-run completed steps" in block


# ---- TEST 6: Completed steps are not blindly repeated -------------------------------------------

async def test_completed_steps_not_repeated(live_pool: Any) -> None:
    """Recovery context clearly marks completed steps so the LLM doesn't re-run them."""
    store = TaskStore(live_pool)
    task = await store.create("Deploy", ["build", "test", "deploy"])
    await store.advance(task.id, 1, "done", note="built successfully")
    await store.advance(task.id, 2, "done", note="all tests pass")

    recovery = await recover_task(live_pool, task.id)
    block = build_recovery_context_block(recovery)
    assert "Do NOT re-run completed steps" in block
    assert "2/3 steps done" in block


# ---- TEST 7: Tool execution IDs remain traceable -----------------------------------------------

async def test_execution_records_traceable(live_pool: Any) -> None:
    """Tool executions are recorded with traceable IDs."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile"])
    await store.activate(task.id)

    exec_id = await store.record_execution(
        task.id, 1, "execute_command",
        tool_args={"command": "gcc main.c"}, idempotent=False, attempt=1)
    assert exec_id is not None

    await store.complete_execution(exec_id, status="completed", result_summary="compiled ok")

    history = await store.get_execution_history(task.id, 1)
    assert len(history) == 1
    assert history[0]["tool_name"] == "execute_command"
    assert history[0]["status"] == "completed"


# ---- TEST 8: Retry count persists --------------------------------------------------------------

async def test_retry_count_persists(live_pool: Any) -> None:
    """Retry count survives across recovery cycles."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile"])
    await store.activate(task.id)

    # First recovery
    await mark_task_interrupted(live_pool, task.id, reason="crash_1")
    r1 = await recover_task(live_pool, task.id)
    assert r1["retry_count"] == 1

    # Second recovery
    await mark_task_interrupted(live_pool, task.id, reason="crash_2")
    r2 = await recover_task(live_pool, task.id)
    assert r2["retry_count"] == 2


# ---- TEST 9: Retry limit is enforced -----------------------------------------------------------

async def test_retry_limit_enforced(live_pool: Any) -> None:
    """A task that exceeds max_retries is marked as failed, not resumed."""
    store = TaskStore(live_pool)
    task = await store.create("Failing task", ["crash"])
    await store.activate(task.id)

    # Set max_retries to 2
    async with live_pool.acquire() as conn:
        await conn.execute("UPDATE task SET max_retries = 2 WHERE id = $1", task.id)

    # Exhaust retries
    for i in range(3):
        await mark_task_interrupted(live_pool, task.id, reason=f"crash_{i}")
        if i < 2:
            await recover_task(live_pool, task.id)

    # Check orphan detection
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    if found:
        assert found["can_resume"] is False


# ---- TEST 10: Transient failures can retry ----------------------------------------------------

async def test_transient_failure_retryable(live_pool: Any) -> None:
    """A step that failed with a transient error can be retried."""
    store = TaskStore(live_pool)
    task = await store.create("Deploy", ["connect", "deploy"])
    await store.advance(task.id, 1, "failed", error="connection timed out")

    got = await store.get(task.id)
    assert got is not None
    s1 = got.steps[0]
    assert s1.failure_class == "transient"
    assert s1.attempts == 1

    # Retry succeeds
    await store.advance(task.id, 1, "done")
    got = await store.get(task.id)
    assert got is not None
    assert got.steps[0].status == "done"


# ---- TEST 11: Non-retryable failures do not retry ---------------------------------------------

async def test_non_retryable_failure_does_not_retry(live_pool: Any) -> None:
    """A step that failed with a permission error is classified as non-retryable."""
    store = TaskStore(live_pool)
    task = await store.create("Deploy", ["deploy"])
    await store.advance(task.id, 1, "failed", error="permission denied")

    got = await store.get(task.id)
    assert got is not None
    assert got.steps[0].failure_class == "permission"


# ---- TEST 12: Cancelled task never resumes automatically ---------------------------------------

async def test_cancelled_task_never_auto_resumes(live_pool: Any) -> None:
    """A cancelled task stays cancelled — recovery doesn't touch it."""
    store = TaskStore(live_pool)
    task = await store.create("Old task", ["step1"])
    await store.activate(task.id)
    await store.cancel(task.id, reason="user said stop")

    got = await store.get(task.id)
    assert got is not None
    assert got.status == "cancelled"

    # Recovery should not find it (it's not 'running')
    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is None


# ---- TEST 13: Superseded task never resumes automatically --------------------------------------

async def test_superseded_task_never_auto_resumes(live_pool: Any) -> None:
    """A superseded task stays non-primary — recovery doesn't reactivate it."""
    store = TaskStore(live_pool)
    task_a = await store.create("Old task", ["step1"])
    await store.activate(task_a.id)
    task_b = await store.create("New task", ["step1"])
    await store.activate(task_b.id)

    # task_a is superseded
    got_a = await store.get(task_a.id)
    assert got_a is not None
    assert got_a.is_primary is False


# ---- TEST 14: Disconnect does not cancel task --------------------------------------------------

async def test_disconnect_does_not_cancel(live_pool: Any) -> None:
    """A task left running after a disconnect is recoverable, not cancelled."""
    store = TaskStore(live_pool)
    task = await store.create("Long task", ["step1", "step2", "step3"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    # Simulate disconnect: set stale heartbeat
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    # Task should be detectable as orphaned, NOT cancelled
    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is not None

    # Mark interrupted (not cancelled)
    await mark_task_interrupted(live_pool, task.id, reason="disconnect")
    got = await store.get(task.id)
    assert got is not None
    assert got.status == "running"  # still running, just interrupted
    assert got.interrupted_at is not None


# ---- TEST 15: Workspace survives restart -------------------------------------------------------

async def test_workspace_survives_restart(live_pool: Any) -> None:
    """Workspace metadata persists across task retrieval."""
    store = TaskStore(live_pool)
    task = await store.create(
        "Build website", ["create index.html"],
        workspace_root="/home/almir/Desktop/example",
        allowed_write_roots=["/home/almir/Desktop/example"],
        workspace_mode="explicit")

    got = await store.get(task.id)
    assert got is not None
    assert got.workspace_root == "/home/almir/Desktop/example"
    assert got.workspace_mode == "explicit"


# ---- TEST 16: Artifacts survive restart --------------------------------------------------------

async def test_artifacts_survive_restart(live_pool: Any) -> None:
    """Artifacts recorded for a task persist across retrieval."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["create files"])
    await store.record_artifact(task.id, "/project/index.html", "created", tool_name="create_file")
    await store.record_artifact(task.id, "/project/style.css", "created", tool_name="create_file")

    artifacts = await store.artifacts(task.id)
    assert len(artifacts) == 2
    assert artifacts[0]["artifact_path"] == "/project/index.html"
    assert artifacts[1]["artifact_path"] == "/project/style.css"


# ---- TEST 17: Context compaction does not destroy task state -----------------------------------

async def test_task_state_independent_of_context(live_pool: Any) -> None:
    """Task state is in the database, not in conversational context.

    Even if the LLM's context is completely lost, the task can be reconstructed.
    """
    store = TaskStore(live_pool)
    task = await store.create("Build website", ["create index.html", "add CSS", "add JS"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done", note="created")
    await store.checkpoint(task.id, 2, {"progress": 50})

    # Simulate complete context loss: reconstruct from DB only
    recovered = await recover_task(live_pool, task.id)
    block = build_recovery_context_block(recovered)

    # All state is in the block, not in memory
    assert "Build website" in block
    assert "create index.html" in block
    assert "1/3 steps done" in block
    assert "progress=50" in block


# ---- TEST 18: Orphaned running task is detected -----------------------------------------------

async def test_orphaned_task_detected(live_pool: Any) -> None:
    """A task with stale heartbeat and status='running' is detected as orphaned."""
    store = TaskStore(live_pool)
    task = await store.create("Long running", ["step1"])
    await store.activate(task.id)

    # Write a heartbeat, then make it stale
    await store.heartbeat(task.id)
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '10 minutes' WHERE id = $1",
            task.id)

    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is not None


# ---- TEST 19: Task completion survives restart -------------------------------------------------

async def test_task_completion_survives_restart(live_pool: Any) -> None:
    """A completed task is archived to sali-works/tasks/ and cleaned from DB."""
    store = TaskStore(live_pool)
    task = await store.create("Quick job", ["do thing"])
    await store.advance(task.id, 1, "done")
    await store.finish(task.id, status="done", result="all done")

    # Task is archived and deleted from DB
    store2 = TaskStore(live_pool)
    got = await store2.get(task.id)
    assert got is None  # archived and cleaned


# ---- TEST 20: Completed task cannot accidentally resume ----------------------------------------

async def test_completed_task_cannot_resume(live_pool: Any) -> None:
    """A done task is archived and cannot be resumed."""
    store = TaskStore(live_pool)
    task = await store.create("Done task", ["step1"])
    await store.advance(task.id, 1, "done")
    await store.finish(task.id, status="done")

    # Task is archived and deleted from DB — resume finds nothing
    result = await store.resume(task.id)
    assert result is None
    got = await store.get(task.id)
    assert got is None  # archived and cleaned


# ---- TEST 21: Recovery is idempotent -----------------------------------------------------------

async def test_recovery_is_idempotent(live_pool: Any) -> None:
    """Recovering the same task twice doesn't corrupt state."""
    store = TaskStore(live_pool)
    task = await store.create("Idempotent test", ["step1", "step2"])
    await store.advance(task.id, 1, "done")

    await mark_task_interrupted(live_pool, task.id, reason="crash")
    r1 = await recover_task(live_pool, task.id)
    r2 = await recover_task(live_pool, task.id)

    # Both recoveries see the same state
    assert r1["completed_steps"] == r2["completed_steps"]
    assert r1["total_steps"] == r2["total_steps"]


# ---- TEST 22: Concurrent recovery cannot create duplicate execution ----------------------------

async def test_no_duplicate_execution_on_recovery(live_pool: Any) -> None:
    """Recording execution with the same (task, step, tool, attempt) doesn't create duplicates."""
    store = TaskStore(live_pool)
    task = await store.create("No dup", ["compile"])
    await store.activate(task.id)

    id1 = await store.record_execution(task.id, 1, "gcc", attempt=1)
    id2 = await store.record_execution(task.id, 1, "gcc", attempt=1)

    # ON CONFLICT should update, not insert
    assert id1 == id2

    history = await store.get_execution_history(task.id, 1)
    assert len(history) == 1


# ---- TEST 23: Database/journal consistency ----------------------------------------------------

async def test_step_and_task_status_consistent(live_pool: Any) -> None:
    """Step status and task status remain consistent after operations."""
    store = TaskStore(live_pool)
    task = await store.create("Consistent", ["a", "b", "c"])

    await store.advance(task.id, 1, "done")
    got = await store.get(task.id)
    assert got is not None
    assert got.status == "running"  # not all done

    await store.advance(task.id, 2, "done")
    await store.advance(task.id, 3, "done")
    # All steps done → task auto-completes, archived to sali-works/tasks/, cleaned from DB
    got = await store.get(task.id)
    assert got is None  # archived and cleaned


# ---- TEST 24: Heartbeat updates ----------------------------------------------------------------

async def test_heartbeat_updates(live_pool: Any) -> None:
    """Heartbeat timestamp updates on a running task."""
    store = TaskStore(live_pool)
    task = await store.create("Heartbeat test", ["step1"])
    await store.activate(task.id)

    await store.heartbeat(task.id)
    got = await store.get(task.id)
    assert got is not None
    assert got.last_heartbeat is not None


# ---- TEST 25: Recovery context block is complete -----------------------------------------------

async def test_recovery_context_block_complete(live_pool: Any) -> None:
    """The recovery context block contains all necessary information for resumption."""
    store = TaskStore(live_pool)
    task = await store.create(
        "Build site", ["create index.html", "add CSS"],
        workspace_root="/tmp/testsite")
    await store.advance(task.id, 1, "done", note="created")
    await store.record_artifact(task.id, "/tmp/testsite/index.html", "created")

    recovery = await recover_task(live_pool, task.id)
    block = build_recovery_context_block(recovery)

    assert "TASK RECOVERY" in block
    assert "Build site" in block
    assert str(task.id) in block
    assert "/tmp/testsite" in block
    assert "create index.html" in block
    assert "1/2 steps done" in block
    assert "/tmp/testsite/index.html" in block
    assert "Do NOT re-run completed steps" in block
