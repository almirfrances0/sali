"""Work must outlive a restart — directive §16, and Almir's own words: "it must survive restart or pc
reboot".

`task.retry_count` was a RESTART counter wearing the name of a failure counter. Recovery incremented it
on every daemon start, nothing in the tree ever reset it, and at 3 the task was marked `failed` with
"exhausted 3 retries". Measured on this machine: task 9df85b0b burned retries 1 and 2 on two restarts
three minutes apart while it was progressing normally, then finished healthy — one restart away from
being destroyed for no reason. These tests pin the corrected meaning.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.tasks.recovery import recover_task
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


class _Pool:
    """The conftest hands out a connection inside a rolled-back transaction; the stores want a pool."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                return conn

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


async def _running_task(store: TaskStore, conn: Any, objective: str = "build the thing") -> Any:
    task = await store.create(objective, ["first step", "second step"])
    await conn.execute("UPDATE task SET status='running', is_primary=true WHERE id=$1", task.id)
    return task


async def _retry_count(conn: Any, task_id: Any) -> int:
    return int(await conn.fetchval("SELECT retry_count FROM task WHERE id=$1", task_id))


async def test_a_clean_restart_costs_the_task_nothing(db_conn: Any) -> None:
    """Nothing was in flight and no step had failed. The process stopped; the task did not fail."""
    store = TaskStore(_Pool(db_conn))
    task = await _running_task(store, db_conn)

    for _ in range(5):                       # five deploys in a row
        await recover_task(_Pool(db_conn), task.id)

    assert await _retry_count(db_conn, task.id) == 0


async def test_an_interruption_mid_tool_still_counts(db_conn: Any) -> None:
    """A tool caught in flight IS wreckage — that is what the budget is for."""
    store = TaskStore(_Pool(db_conn))
    task = await _running_task(store, db_conn)
    await db_conn.execute(
        "INSERT INTO task_execution (task_id, step_seq, tool_name, status, attempt) "
        "VALUES ($1, 1, 'execute_command', 'running', 1)", task.id)

    await recover_task(_Pool(db_conn), task.id)

    assert await _retry_count(db_conn, task.id) == 1


async def test_progress_clears_the_budget(db_conn: Any) -> None:
    """A completed step is proof this task is not in a crash loop, so the budget starts over."""
    store = TaskStore(_Pool(db_conn))
    task = await _running_task(store, db_conn)
    await db_conn.execute("UPDATE task SET retry_count = 2 WHERE id=$1", task.id)

    await store.advance(task.id, 1, "done")

    assert await _retry_count(db_conn, task.id) == 0


async def test_a_failing_step_does_not_clear_the_budget(db_conn: Any) -> None:
    """Only real progress resets it — otherwise a task could fail forever and never exhaust anything."""
    store = TaskStore(_Pool(db_conn))
    task = await _running_task(store, db_conn)
    await db_conn.execute("UPDATE task SET retry_count = 2 WHERE id=$1", task.id)

    await store.advance(task.id, 1, "failed", error="nope")

    assert await _retry_count(db_conn, task.id) == 2


async def test_the_boot_loop_ceiling_still_stops_a_task_that_never_progresses(db_conn: Any) -> None:
    """The counter exists to stop a task that kills the daemon on every start from being re-adopted
    forever. Sharpening it must not remove that."""
    from sali.tasks.recovery import detect_orphaned_tasks

    store = TaskStore(_Pool(db_conn))
    task = await _running_task(store, db_conn)

    for attempt in range(3):
        await db_conn.execute(
            "INSERT INTO task_execution (task_id, step_seq, tool_name, status, attempt) "
            "VALUES ($1, 1, 'execute_command', 'running', $2)", task.id, attempt + 1)
        await recover_task(_Pool(db_conn), task.id)

    assert await _retry_count(db_conn, task.id) == 3
    await db_conn.execute(
        "UPDATE task SET last_heartbeat = now() - interval '1 hour' WHERE id=$1", task.id)
    orphans = await detect_orphaned_tasks(_Pool(db_conn))
    mine = [o for o in orphans if o["task_id"] == str(task.id)]
    assert mine and mine[0]["can_resume"] is False, "a task that never progresses must still be stopped"
