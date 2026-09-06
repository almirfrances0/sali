"""Durable task execution — advanced integration tests.

Tests the gaps not covered by test_durability.py: step tracking in the loop,
sali-works integration, periodic heartbeat during execution, and end-to-end
crash-recovery scenarios that prove the system doesn't depend on LLM context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.ids import new_id
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.logger import (
    append_event,
    save_checkpoint,
    save_task_meta,
    save_task_record,
)
from sali.tasks.recovery import (
    build_recovery_context_block,
    detect_orphaned_tasks,
    mark_task_interrupted,
    recover_task,
)
from sali.tasks.store import TaskStore
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


# ---- helpers ------------------------------------------------------------------------------------

def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))


# A crash is only a retry when it LOST something. `retry_count` used to rise on every recovery cycle,
# including one where nothing was in flight — so routine daemon restarts consumed a healthy task's whole
# budget and marked it "exhausted 3 retries" (measured live: task 9df85b0b burned 2 of 3 on two restarts
# three minutes apart while progressing normally). These tests still assert that the budget persists and
# is enforced; they now stage a genuine crash — a tool caught mid-flight — instead of an empty one.
async def _crash_mid_tool(pool: Any, task_id: Any, attempt: int = 1) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task_execution (task_id, step_seq, tool_name, status, attempt) "
            "VALUES ($1, 1, 'execute_command', 'running', $2)", task_id, attempt)


# ---- TEST: current_step returns the running step -----------------------------------------------

async def test_current_step_returns_running_step(live_pool: Any) -> None:
    """current_step returns the seq of the step with status='running'."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile", "test", "deploy"])
    await store.advance(task.id, 1, "running")

    step = await store.current_step(task.id)
    assert step == 1


async def test_current_step_returns_none_when_no_running_step(live_pool: Any) -> None:
    """current_step returns None when no step is running."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile", "test"])

    step = await store.current_step(task.id)
    assert step is None


async def test_current_step_returns_first_running(live_pool: Any) -> None:
    """If somehow multiple steps are running, returns the first by seq."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile", "test"])
    await store.advance(task.id, 1, "running")
    # Directly set step 2 to running too (shouldn't happen normally)
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task_step SET status='running' WHERE task_id=$1 AND seq=2", task.id)

    step = await store.current_step(task.id)
    assert step == 1  # first by seq order


# ---- TEST: execution recorded against correct step ---------------------------------------------

async def test_execution_recorded_against_correct_step(live_pool: Any) -> None:
    """The loop records executions against the actual current step, not hardcoded 0."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile", "test"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Record execution — should go to step 1
    exec_id = await store.record_execution(task.id, 1, "execute_command",
                                            tool_args={"command": "gcc main.c"})
    assert exec_id is not None

    history = await store.get_execution_history(task.id, 1)
    assert len(history) == 1
    assert history[0]["tool_name"] == "execute_command"

    # No execution on step 0 (which doesn't exist)
    history_0 = await store.get_execution_history(task.id, 0)
    assert len(history_0) == 0


# ---- TEST: sali-works task meta is written ----------------------------------------------------

async def test_save_task_meta_creates_file(tmp_path: Path) -> None:
    """save_task_meta creates meta.json in sali-works/task-XXXX/."""
    task_id = new_id()
    task_dir = tmp_path / "task-test"
    task_dir.mkdir()
    with patch("sali.tasks.logger._task_dir", return_value=task_dir):
        save_task_meta(task_id, "Build website", ["create index.html", "add CSS"])

    meta_path = task_dir / "meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["objective"] == "Build website"
    assert meta["steps"] == ["create index.html", "add CSS"]
    assert meta["status"] == "open"
    assert meta["task_id"] == str(task_id)


# ---- TEST: sali-works event log is appended ----------------------------------------------------

async def test_append_event_creates_jsonl(tmp_path: Path) -> None:
    """append_event appends to events.jsonl in sali-works/task-XXXX/."""
    task_id = new_id()
    task_dir = tmp_path / "task-test"
    task_dir.mkdir()
    with patch("sali.tasks.logger._task_dir", return_value=task_dir):
        append_event(task_id, "step_advanced", {"step": 1, "status": "done"})
        append_event(task_id, "tool_executed", {"tool": "execute_command", "ok": True})

    events_path = task_dir / "events.jsonl"
    assert events_path.exists()
    lines = events_path.read_text().strip().split("\n")
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    assert e1["event"] == "step_advanced"
    e2 = json.loads(lines[1])
    assert e2["event"] == "tool_executed"


# ---- TEST: sali-works checkpoint is saved ------------------------------------------------------

async def test_save_checkpoint_writes_progress(tmp_path: Path) -> None:
    """save_checkpoint writes checkpoint data to progress.json."""
    task_id = new_id()
    task_dir = tmp_path / "task-test"
    task_dir.mkdir()
    with patch("sali.tasks.logger._task_dir", return_value=task_dir):
        save_checkpoint(task_id, 2, {"downloaded": 50, "of": 100})
        save_checkpoint(task_id, 2, {"downloaded": 75, "of": 100})  # update

    progress_path = task_dir / "progress.json"
    assert progress_path.exists()
    progress = json.loads(progress_path.read_text())
    assert "2" in progress["checkpoints"]
    assert progress["checkpoints"]["2"]["data"]["downloaded"] == 75


# ---- TEST: sali-works full task record ---------------------------------------------------------

async def test_save_task_record_creates_all_files(tmp_path: Path) -> None:
    """save_task_record creates meta.json, progress.json, executions.json, artifacts.json."""
    task_id = new_id()
    task_dir = tmp_path / "task-test"
    task_dir.mkdir()
    with patch("sali.tasks.logger._task_dir", return_value=task_dir):
        save_task_record(
            task_id,
            {"objective": "Build site", "status": "running", "workspace_root": None,
             "workspace_mode": "none", "is_primary": True, "max_retries": 3,
             "retry_count": 0, "created_at": "2025-01-01", "updated_at": "2025-01-01"},
            [{"seq": 1, "description": "compile", "status": "done"},
             {"seq": 2, "description": "test", "status": "running"}],
            executions=[{"tool": "gcc", "status": "completed"}],
            artifacts=[{"path": "/project/main.o", "type": "created"}])

    assert (task_dir / "meta.json").exists()
    assert (task_dir / "progress.json").exists()
    assert (task_dir / "executions.json").exists()
    assert (task_dir / "artifacts.json").exists()

    meta = json.loads((task_dir / "meta.json").read_text())
    assert meta["objective"] == "Build site"
    progress = json.loads((task_dir / "progress.json").read_text())
    assert len(progress["steps"]) == 2


# ---- TEST: end-to-end crash recovery with context reconstruction --------------------------------

async def test_end_to_end_crash_recovery(live_pool: Any) -> None:
    """Simulate: start task → do steps → crash → restart → reconstruct from DB → continue."""
    store = TaskStore(live_pool)

    # Phase 1: Task is created and partially completed
    task = await store.create("Build website", ["create index.html", "add CSS", "add JS"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done", note="created index.html")
    await store.checkpoint(task.id, 2, {"files_written": 2, "of": 5})

    # Phase 2: Simulate crash — set stale heartbeat
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    # Phase 3: Restart — detect orphaned tasks
    orphaned = await detect_orphaned_tasks(live_pool)
    assert len(orphaned) >= 1
    found = next(o for o in orphaned if o["task_id"] == str(task.id))
    assert found["can_resume"] is True

    # Phase 4: Mark interrupted and recover
    await mark_task_interrupted(live_pool, task.id, reason="process_crash")
    recovery = await recover_task(live_pool, task.id)

    # Phase 5: Verify recovery context is complete
    assert recovery["completed_steps"] == 1
    assert recovery["total_steps"] == 3
    assert recovery["retry_count"] == 1

    block = build_recovery_context_block(recovery)
    assert "Build website" in block
    assert "1/3 steps done" in block
    assert "Do NOT re-run completed steps" in block

    # Phase 6: Continue from checkpoint — step 2 still has mid-step progress
    got = await store.get(task.id)
    assert got is not None
    s2 = next(s for s in got.steps if s.seq == 2)
    assert s2.checkpoint == {"files_written": 2, "of": 5}

    # Phase 7: Complete the task — auto-archives to sali-works/tasks/, cleans from DB
    await store.advance(task.id, 2, "done", note="CSS added")
    await store.advance(task.id, 3, "done", note="JS added")
    got = await store.get(task.id)
    assert got is None  # archived and cleaned


# ---- TEST: completed task cannot be accidentally re-executed after recovery ---------------------

async def test_completed_task_stays_completed_after_recovery(live_pool: Any) -> None:
    """A completed task is archived to sali-works/tasks/ and deleted from DB — not orphaned."""
    store = TaskStore(live_pool)
    task = await store.create("Quick fix", ["fix bug"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")
    await store.finish(task.id, status="done", result="fixed")

    # Task is archived and deleted — orphan detection can't find it
    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is None  # done tasks are not detected as orphaned


# ---- TEST: cancelled task stays cancelled through recovery --------------------------------------

async def test_cancelled_task_stays_cancelled(live_pool: Any) -> None:
    """A cancelled task is never auto-resumed, even after process restart."""
    store = TaskStore(live_pool)
    task = await store.create("Cancelled work", ["step1", "step2"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")
    await store.cancel(task.id, reason="user said stop")

    # Simulate restart: orphan detection should not find it
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is None

    # Status stays cancelled
    got = await store.get(task.id)
    assert got is not None
    assert got.status == "cancelled"


# ---- TEST: superseded task stays superseded through recovery ------------------------------------

async def test_superseded_task_stays_superseded(live_pool: Any) -> None:
    """A superseded task is not auto-resumed by recovery."""
    store = TaskStore(live_pool)
    task_a = await store.create("Old task", ["step1"])
    await store.activate(task_a.id)
    task_b = await store.create("New task", ["step1"])
    # Use supersede() to set the superseded_by relationship (activate() alone doesn't)
    await store.supersede(task_a.id, task_b.id, reason="new task")
    await store.activate(task_b.id)

    # task_a is superseded, not orphaned
    got_a = await store.get(task_a.id)
    assert got_a is not None
    assert got_a.is_primary is False
    assert got_a.superseded_by == task_b.id


# ---- TEST: disconnect does not cancel task (heartbeat stale but recoverable) --------------------

async def test_disconnect_task_remains_recoverable(live_pool: Any) -> None:
    """After disconnect, the task is orphaned but NOT cancelled — it can be recovered."""
    store = TaskStore(live_pool)
    task = await store.create("Long build", ["compile", "link", "package"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done", note="compiled")

    # Simulate disconnect: stale heartbeat
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    orphaned = await detect_orphaned_tasks(live_pool)
    found = next(o for o in orphaned if o["task_id"] == str(task.id))
    assert found["can_resume"] is True

    # Mark interrupted (NOT cancelled)
    await mark_task_interrupted(live_pool, task.id, reason="disconnect")
    got = await store.get(task.id)
    assert got is not None
    assert got.status == "running"  # still running, just interrupted
    assert got.interrupted_at is not None

    # Recover
    recovery = await recover_task(live_pool, task.id)
    assert recovery["completed_steps"] == 1
    assert recovery["total_steps"] == 3


# ---- TEST: recovery preserves workspace ----------------------------------------------------------------------------------

async def test_recovery_preserves_workspace(live_pool: Any) -> None:
    """After recovery, the workspace root is unchanged."""
    store = TaskStore(live_pool)
    task = await store.create(
        "Build site", ["create index.html"],
        workspace_root="/home/almir/Desktop/example",
        allowed_write_roots=["/home/almir/Desktop/example"],
        workspace_mode="explicit")
    await store.activate(task.id)

    await mark_task_interrupted(live_pool, task.id, reason="crash")
    recovery = await recover_task(live_pool, task.id)
    assert recovery["workspace_root"] == "/home/almir/Desktop/example"

    block = build_recovery_context_block(recovery)
    assert "/home/almir/Desktop/example" in block


# ---- TEST: recovery preserves artifacts -----------------------------------------------------------------------------------

async def test_recovery_preserves_artifacts(live_pool: Any) -> None:
    """After recovery, artifacts are included in the recovery context."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["create files", "deploy"])
    await store.activate(task.id)
    await store.record_artifact(task.id, "/project/index.html", "created", tool_name="create_file")
    await store.record_artifact(task.id, "/project/style.css", "created", tool_name="create_file")

    await mark_task_interrupted(live_pool, task.id, reason="crash")
    recovery = await recover_task(live_pool, task.id)
    assert len(recovery["artifacts"]) == 2

    block = build_recovery_context_block(recovery)
    assert "/project/index.html" in block
    assert "/project/style.css" in block


# ---- TEST: execution history survives recovery ------------------------------------------------------------------------------

async def test_execution_history_survives_recovery(live_pool: Any) -> None:
    """Tool execution records survive crash and are visible after recovery."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    exec_id = await store.record_execution(
        task.id, 1, "execute_command",
        tool_args={"command": "gcc main.c"}, idempotent=False, attempt=1)
    await store.complete_execution(exec_id, status="completed", result_summary="compiled ok")

    # Simulate crash + recovery
    await mark_task_interrupted(live_pool, task.id, reason="crash")
    await recover_task(live_pool, task.id)

    # Execution history is still there
    history = await store.get_execution_history(task.id, 1)
    assert len(history) == 1
    assert history[0]["tool_name"] == "execute_command"
    assert history[0]["status"] == "completed"


# ---- TEST: idempotency check prevents duplicate work -----------------------------------------------------------------------

async def test_idempotency_check_prevents_rerun(live_pool: Any) -> None:
    """If a tool already completed for a step, has_completed_execution returns True."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["create file"])
    await store.activate(task.id)

    # Record a completed execution
    exec_id = await store.record_execution(
        task.id, 1, "create_file", tool_args={"path": "/project/index.html"})
    await store.complete_execution(exec_id, status="completed")

    # Idempotency check should say "already done"
    assert await store.has_completed_execution(task.id, 1, "create_file") is True
    # Different tool is not done
    assert await store.has_completed_execution(task.id, 1, "execute_command") is False


# ---- TEST: retry count increments across recovery cycles -------------------------------------------------------------------

async def test_retry_count_increments_across_recoveries(live_pool: Any) -> None:
    """Each recovery cycle increments retry_count."""
    store = TaskStore(live_pool)
    task = await store.create("Fragile task", ["step1"])
    await store.activate(task.id)

    for i in range(3):
        await _crash_mid_tool(live_pool, task.id, i + 1)
        await mark_task_interrupted(live_pool, task.id, reason=f"crash_{i}")
        recovery = await recover_task(live_pool, task.id)
        assert recovery["retry_count"] == i + 1


# ---- TEST: max retries exhausted → task fails ------------------------------------------------------------------------------

async def test_max_retries_exhausted_fails_task(live_pool: Any) -> None:
    """When retry_count >= max_retries, orphan detection marks can_resume=False."""
    store = TaskStore(live_pool)
    task = await store.create("Brittle task", ["step1"])
    await store.activate(task.id)

    # Set max_retries to 1
    async with live_pool.acquire() as conn:
        await conn.execute("UPDATE task SET max_retries = 1 WHERE id = $1", task.id)

    # First crash + recovery — a real one, with a tool caught in flight
    await _crash_mid_tool(live_pool, task.id, 1)
    await mark_task_interrupted(live_pool, task.id, reason="crash_1")
    await recover_task(live_pool, task.id)  # retry_count → 1

    # Second crash — now at max
    await mark_task_interrupted(live_pool, task.id, reason="crash_2")

    # Set stale heartbeat
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '5 minutes' WHERE id = $1",
            task.id)

    orphaned = await detect_orphaned_tasks(live_pool)
    found = next((o for o in orphaned if o["task_id"] == str(task.id)), None)
    assert found is not None
    assert found["can_resume"] is False


# ---- TEST: database/journal consistency after multiple operations ------------------------------

async def test_db_consistency_after_complex_sequence(live_pool: Any) -> None:
    """After a complex sequence of operations, task and step states remain consistent."""
    store = TaskStore(live_pool)
    task = await store.create("Complex task", ["a", "b", "c", "d"])
    await store.activate(task.id)

    # Step 1: done
    await store.advance(task.id, 1, "done")
    # Step 2: fail then retry then done
    await store.advance(task.id, 2, "failed", error="timeout")
    await store.advance(task.id, 2, "done")
    # Step 3: running with checkpoint
    await store.advance(task.id, 3, "running")
    await store.checkpoint(task.id, 3, {"progress": 50})

    # Verify consistency
    got = await store.get(task.id)
    assert got is not None
    assert got.status == "running"
    assert got.steps[0].status == "done"
    assert got.steps[1].status == "done"
    assert got.steps[1].attempts == 1  # failure count preserved
    assert got.steps[2].status == "running"
    assert got.steps[2].checkpoint == {"progress": 50}
    assert got.steps[3].status == "pending"

    # Complete remaining — task auto-completes, archived, cleaned from DB
    await store.advance(task.id, 3, "done")
    await store.advance(task.id, 4, "done")
    got = await store.get(task.id)
    assert got is None  # archived and cleaned


# ---- TEST: concurrent task creation doesn't duplicate -----------------------------------------------------------------------

async def test_concurrent_task_creation_no_duplicate(live_pool: Any) -> None:
    """Two tasks can coexist; only one is primary."""
    store = TaskStore(live_pool)
    t1 = await store.create("Task 1", ["step1"])
    t2 = await store.create("Task 2", ["step1"])

    await store.activate(t1.id)
    a1 = await store.active_task()
    assert a1 is not None and a1.id == t1.id

    await store.activate(t2.id)
    a2 = await store.active_task()
    assert a2 is not None and a2.id == t2.id

    # t1 is no longer primary
    got1 = await store.get(t1.id)
    assert got1 is not None
    assert got1.is_primary is False


# ---- TEST: task one_line includes checkpoint info for recovery ---------------------------------

async def test_one_line_includes_checkpoint_for_recovery(live_pool: Any) -> None:
    """The one_line() resume view includes mid-step checkpoint info."""
    store = TaskStore(live_pool)
    task = await store.create("Bulk download", ["download 100 files"])
    await store.checkpoint(task.id, 1, {"downloaded": 42, "of": 100})

    got = await store.get(task.id)
    assert got is not None
    line = got.one_line()
    assert "resuming mid-step" in line
    assert "downloaded=42" in line


# ---- TEST: recovery context block includes step checkpoints ------------------------------------

async def test_recovery_block_includes_checkpoints(live_pool: Any) -> None:
    """The recovery context block shows step checkpoint data."""
    store = TaskStore(live_pool)
    task = await store.create("Process data", ["fetch", "transform", "load"])
    await store.advance(task.id, 1, "done", note="fetched")
    await store.checkpoint(task.id, 2, {"rows_processed": 5000, "of": 10000})

    recovery = await recover_task(live_pool, task.id)
    block = build_recovery_context_block(recovery)
    assert "rows_processed" in block
    assert "checkpoint" in block.lower() or "resuming" in block.lower()


# ---- TEST: heartbeat doesn't affect non-running tasks -----------------------------------------------------------------------

async def test_heartbeat_only_updates_running_tasks(live_pool: Any) -> None:
    """Heartbeat only updates tasks with status='running'."""
    store = TaskStore(live_pool)
    task = await store.create("Paused task", ["step1"])
    # Don't activate — status stays 'open'

    await store.heartbeat(task.id)  # should be a no-op
    got = await store.get(task.id)
    assert got is not None
    assert got.last_heartbeat is None  # not updated because status is 'open'


# ---- TEST: multiple tools on same step tracked correctly --------------------------------------------------------------------

async def test_multiple_tools_on_same_step(live_pool: Any) -> None:
    """Multiple tool executions on the same step are all recorded."""
    store = TaskStore(live_pool)
    task = await store.create("Build", ["compile"])
    await store.activate(task.id)

    e1 = await store.record_execution(task.id, 1, "execute_command",
                                       tool_args={"command": "gcc -c a.c"}, attempt=1)
    e2 = await store.record_execution(task.id, 1, "execute_command",
                                       tool_args={"command": "gcc -c b.c"}, attempt=2)

    assert e1 != e2  # different execution records

    history = await store.get_execution_history(task.id, 1)
    assert len(history) == 2
