"""Progress tracking and watchdog tests.

Tests that the system correctly distinguishes heartbeat from progress,
detects potentially stuck tasks, and records meaningful progress.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from sali.tasks.store import TaskStore
from sali.tasks.watchdog import TaskWatchdog, WatchdogConfig

pytestmark = pytest.mark.db


# ── 1. Heartbeat alone does not count as meaningful progress ──────────────────

async def test_heartbeat_does_not_update_progress(live_pool: Any) -> None:
    """Heartbeat updates last_heartbeat but NOT last_progress_at."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Record initial progress
    await store.record_progress(task.id, "step_advance")
    got = await store.get(task.id)
    assert got is not None
    first_progress = got.last_progress_at

    await asyncio.sleep(0.05)

    # Heartbeat should NOT update progress
    await store.heartbeat(task.id)
    got = await store.get(task.id)
    assert got is not None
    assert got.last_heartbeat is not None
    # last_progress_at should be unchanged (heartbeat doesn't count as progress)
    assert got.last_progress_at == first_progress


# ── 2. Successful tool execution counts as progress ──────────────────────────

async def test_tool_success_records_progress(live_pool: Any) -> None:
    """record_progress with tool_success updates last_progress_at."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    await store.record_progress(task.id, "tool_success", tool_name="execute_command")
    got = await store.get(task.id)
    assert got is not None
    assert got.last_progress_at is not None
    assert got.last_progress_type == "tool_success"


# ── 3. Step advancement counts as progress ───────────────────────────────────

async def test_step_advancement_records_progress(live_pool: Any) -> None:
    """advance() updates last_progress_at."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1", "step2"])
    await store.activate(task.id)

    await store.advance(task.id, 1, "done")
    got = await store.get(task.id)
    assert got is not None
    assert got.last_progress_at is not None
    assert got.last_progress_type == "step_advance"


# ── 4. Checkpoint persistence counts as progress ─────────────────────────────

async def test_checkpoint_records_progress(live_pool: Any) -> None:
    """checkpoint() updates last_progress_at."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    await store.checkpoint(task.id, 1, {"processed": 50})
    got = await store.get(task.id)
    assert got is not None
    assert got.last_progress_at is not None
    assert got.last_progress_type == "checkpoint"


# ── 5. Active tool name is tracked ───────────────────────────────────────────

async def test_active_tool_tracking(live_pool: Any) -> None:
    """set_active_tool tracks which tool is executing."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)

    await store.set_active_tool(task.id, "execute_command")
    got = await store.get(task.id)
    assert got is not None
    assert got.active_tool_name == "execute_command"

    await store.set_active_tool(task.id, None)
    got = await store.get(task.id)
    assert got is not None
    assert got.active_tool_name is None


# ── 6. Health status defaults to healthy ─────────────────────────────────────

async def test_health_status_defaults_healthy(live_pool: Any) -> None:
    """New tasks have health_status='healthy'."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    assert task.health_status == "healthy"


# ── 7. Watchdog classifies active tool as active_tool ────────────────────────

async def test_watchdog_classifies_active_tool(live_pool: Any) -> None:
    """Task with active tool is classified as active_tool, not stuck."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")
    await store.set_active_tool(task.id, "execute_command")

    config = WatchdogConfig(
        progress_timeout=timedelta(seconds=1),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(seconds=5),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "active_tool"


# ── 8. Watchdog classifies healthy task correctly ────────────────────────────

async def test_watchdog_classifies_healthy(live_pool: Any) -> None:
    """Task with recent progress is classified as healthy."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")
    await store.record_progress(task.id, "tool_success")

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=30),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=5),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "healthy"


# ── 9. Watchdog detects potentially stuck task ───────────────────────────────

async def test_watchdog_detects_potentially_stuck(live_pool: Any) -> None:
    """Task with stale progress and no active tool is potentially_stuck."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Set progress to long ago
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_progress_at = now() - interval '1 hour', "
            "  last_heartbeat = now() WHERE id = $1",
            task.id)

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=5),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=10),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "potentially_stuck"


# ── 10. Watchdog detects orphaned task ───────────────────────────────────────

async def test_watchdog_detects_orphaned(live_pool: Any) -> None:
    """Task with stale heartbeat is orphaned."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Set heartbeat to long ago
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() - interval '1 hour' WHERE id = $1",
            task.id)

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=30),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=2),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "orphaned"


# ── 11. Watchdog does not affect completed tasks ─────────────────────────────

async def test_watchdog_ignores_completed_tasks(live_pool: Any) -> None:
    """Completed tasks are not classified by watchdog."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")
    await store.finish(task.id, status="done")

    # Task is archived — watchdog should not find it
    config = WatchdogConfig()
    watchdog = TaskWatchdog(live_pool, config)
    summary = await watchdog.get_health_summary()
    task_ids = [r["id"] for r in summary]
    assert task.id not in task_ids


# ── 12. Watchdog does not kill tasks ─────────────────────────────────────────

async def test_watchdog_does_not_modify_task_status(live_pool: Any) -> None:
    """Watchdog only updates health_status, not task.status."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Set stale progress
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_progress_at = now() - interval '1 hour', "
            "  last_heartbeat = now() WHERE id = $1",
            task.id)

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=5),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=10),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.status == "running"  # status unchanged
    assert got.health_status == "potentially_stuck"  # only health changed


# ── 13. Progress timestamp survives restart ───────────────────────────────────

async def test_progress_survives_restart(live_pool: Any) -> None:
    """Progress fields persist across store instances (simulated restart)."""
    store1 = TaskStore(live_pool)
    task = await store1.create("Test task", ["step1"])
    await store1.activate(task.id)
    await store1.advance(task.id, 1, "running")
    await store1.record_progress(task.id, "tool_success", tool_name="gcc")

    # Simulate restart — new store instance
    store2 = TaskStore(live_pool)
    got = await store2.get(task.id)
    assert got is not None
    assert got.last_progress_at is not None
    assert got.last_progress_type == "tool_success"


# ── 14. one_line includes health status ──────────────────────────────────────

async def test_one_line_includes_health(live_pool: Any) -> None:
    """one_line() includes health_status when not healthy."""
    store = TaskStore(live_pool)
    task = await store.create("Build site", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Set health to potentially_stuck
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET health_status = 'potentially_stuck' WHERE id = $1",
            task.id)

    got = await store.get(task.id)
    assert got is not None
    line = got.one_line()
    assert "potentially_stuck" in line


# ── 15. Watchdog thresholds are configurable ─────────────────────────────────

async def test_watchdog_configurable_thresholds(live_pool: Any) -> None:
    """Watchdog respects custom thresholds."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")
    await store.record_progress(task.id, "tool_success")

    # Very short timeout — should NOT trigger (progress is recent)
    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=30),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=5),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "healthy"


# ── 16. Watchdog starts and stops cleanly ─────────────────────────────────────

async def test_watchdog_start_stop(live_pool: Any) -> None:
    """Watchdog starts and stops without leaking tasks."""
    config = WatchdogConfig(check_interval=timedelta(seconds=1))
    watchdog = TaskWatchdog(live_pool, config)

    await watchdog.start()
    assert watchdog._task is not None
    assert not watchdog._task.done()

    await watchdog.stop()
    assert watchdog._task is None or watchdog._task.done()


# ── 17. Watchdog emits health transition event ───────────────────────────────

async def test_watchdog_emits_health_transition_event(live_pool: Any) -> None:
    """Watchdog emits a task.health_changed event when health transitions."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_progress_at = now() - interval '1 hour', "
            "  last_heartbeat = now() WHERE id = $1",
            task.id)

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=5),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=10),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.health_changed' "
            "AND subject_id = $1", task.id)
        assert len(events) >= 1
        payload = dict(events[0]["payload"])
        assert payload["old_health"] == "healthy"
        assert payload["new_health"] == "potentially_stuck"


# ── 18. Watchdog no repeated events ───────────────────────────────────────────

async def test_watchdog_no_repeated_events(live_pool: Any) -> None:
    """Watchdog does not emit events when health hasn't changed."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")
    await store.record_progress(task.id, "tool_success")

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=30),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=5),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()
    await watchdog._check_all()

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.health_changed' "
            "AND subject_id = $1", task.id)
        assert len(events) == 0


# ── 19. Progress event on step advance ────────────────────────────────────────

async def test_progress_event_on_step_advance(live_pool: Any) -> None:
    """Step advancement emits task.progress event."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1", "step2"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.progress' "
            "AND subject_id = $1", task.id)
        assert len(events) >= 1
        payload = dict(events[0]["payload"])
        assert payload["progress_type"] == "step_advance"


# ── 20. Step completed event ──────────────────────────────────────────────────

async def test_step_completed_event(live_pool: Any) -> None:
    """Step completion emits task.step.completed event."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1", "step2"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.step.completed' "
            "AND subject_id = $1", task.id)
        assert len(events) >= 1


# ── 21. Step failed event ─────────────────────────────────────────────────────

async def test_step_failed_event(live_pool: Any) -> None:
    """Step failure emits task.step.failed event."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1", "step2"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "failed", error="connection timed out")

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.step.failed' "
            "AND subject_id = $1", task.id)
        assert len(events) >= 1
        payload = dict(events[0]["payload"])
        assert "timed out" in payload["error"]


# ── 22. Heartbeat no progress event ───────────────────────────────────────────

async def test_heartbeat_no_progress_event(live_pool: Any) -> None:
    """Heartbeat does not emit task.progress events."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    await store.heartbeat(task.id)

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.progress' "
            "AND subject_id = $1", task.id)
        assert len(events) <= 1


# ── 23. Progress events have identifiers ──────────────────────────────────────

async def test_progress_event_has_identifiers(live_pool: Any) -> None:
    """Progress events contain task_id and progress_type."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.record_progress(task.id, "tool_success", tool_name="gcc")

    async with live_pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT * FROM event WHERE event_type = 'task.progress' "
            "AND subject_id = $1", task.id)
        assert len(events) >= 1
        payload = dict(events[0]["payload"])
        assert payload["progress_type"] == "tool_success"
        assert payload["tool_name"] == "gcc"


# ── 24. Health transition stuck to healthy ────────────────────────────────────

async def test_health_transition_stuck_to_healthy(live_pool: Any) -> None:
    """Task transitions from potentially_stuck back to healthy on progress."""
    store = TaskStore(live_pool)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_progress_at = now() - interval '1 hour', "
            "  last_heartbeat = now() WHERE id = $1",
            task.id)

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=5),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=10),
    )
    watchdog = TaskWatchdog(live_pool, config)
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "potentially_stuck"

    await store.record_progress(task.id, "tool_success")
    await watchdog._check_all()

    got = await store.get(task.id)
    assert got is not None
    assert got.health_status == "healthy"
