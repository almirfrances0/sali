"""Turn 1 of the Work-system audit: the archive path preserves the task row.

Before this turn: `_archive_and_cleanup` ran DELETE FROM task, and CASCADE removed
task_step, task_execution, task_review, task_artifact. Result: 25 historical
`task.created` events but 0 rows across all four tables - nothing to power §15
follow-up detection, §13 review history, or iOS history.

After: the DELETE becomes `UPDATE task SET archived_at = now()`. The JSON snapshot
still writes to disk (grep-able export). Operational queries filter by status so
archived rows do not leak into "what is Sali doing right now?" surfaces.

These tests pin the new contract end-to-end against a real Postgres via `live_pool`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from sali.tasks.store import TaskStore


async def _clear(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE sali.task CASCADE")


async def _seed_completed_task_with_children(pool) -> str:
    """Insert one done task with a step, an execution, an artifact, and a review row -
    the four child tables the old DELETE+CASCADE was destroying."""
    task_id = uuid4()
    step_id = uuid4()
    exec_id = uuid4()
    artifact_id = uuid4()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sali.task (id, objective, status, created_at, started_at, "
            "  completed_at, updated_at) VALUES ($1, $2, 'done', $3, $3, $3, $3)",
            task_id, "Ship the metrics dashboard", now)
        await conn.execute(
            "INSERT INTO sali.task_step (task_id, seq, description, status, verified, "
            "  verified_by) VALUES ($1, 1, 'design the layout', 'done', TRUE, $2)",
            task_id, exec_id)
        await conn.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'code_edit', 'completed', 1, TRUE, $3, $3)",
            exec_id, task_id, now)
        await conn.execute(
            "INSERT INTO sali.task_artifact (id, task_id, artifact_path, artifact_type, "
            "  tool_name, created_at) VALUES ($1, $2, '/tmp/nope', 'created', 'code_edit', $3)",
            artifact_id, task_id, now)
        await conn.execute(
            "INSERT INTO sali.task_review (task_id, attempt, status, reviewer_type, "
            "  summary, requirements_checked, failures, recommendations, passed_count, "
            "  failed_count, unknown_count, started_at, completed_at) "
            "VALUES ($1, 1, 'passed', 'deterministic', 'ok', '[]'::jsonb, '[]'::jsonb, "
            "  '[]'::jsonb, 0, 0, 0, $2, $2)",
            task_id, now)
    return str(task_id)


@pytest.mark.asyncio
async def test_archive_preserves_task_row_with_archived_at(live_pool) -> None:
    """The row survives archive; archived_at is set; status stays done."""
    await _clear(live_pool)
    task_id = await _seed_completed_task_with_children(live_pool)
    await TaskStore(live_pool)._archive_and_cleanup(task_id)  # type: ignore[arg-type]

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text AS id, status, archived_at, is_primary "
            "FROM sali.task WHERE id = $1", task_id)
    assert row is not None, "task row was deleted (regression to old behavior)"
    assert row["status"] == "done"
    assert row["archived_at"] is not None
    assert row["is_primary"] is False


@pytest.mark.asyncio
async def test_archive_preserves_all_child_tables(live_pool) -> None:
    """The four CASCADE-linked tables (step/execution/artifact/review) all survive too."""
    await _clear(live_pool)
    task_id = await _seed_completed_task_with_children(live_pool)
    await TaskStore(live_pool)._archive_and_cleanup(task_id)

    async with live_pool.acquire() as conn:
        counts = {
            "task_step":      await conn.fetchval(
                "SELECT count(*) FROM sali.task_step WHERE task_id = $1", task_id),
            "task_execution": await conn.fetchval(
                "SELECT count(*) FROM sali.task_execution WHERE task_id = $1", task_id),
            "task_artifact":  await conn.fetchval(
                "SELECT count(*) FROM sali.task_artifact WHERE task_id = $1", task_id),
            "task_review":    await conn.fetchval(
                "SELECT count(*) FROM sali.task_review WHERE task_id = $1", task_id),
        }
    assert counts == {"task_step": 1, "task_execution": 1, "task_artifact": 1,
                      "task_review": 1}, f"CASCADE ate children: {counts}"


@pytest.mark.asyncio
async def test_archived_task_does_not_leak_into_active_scans(live_pool) -> None:
    """Operational queries (attention_snapshot, active_task, recovery scan) already
    filter by status; this pins that behavior after the migration so no accidental leak."""
    await _clear(live_pool)
    task_id = await _seed_completed_task_with_children(live_pool)
    await TaskStore(live_pool)._archive_and_cleanup(task_id)

    active = await TaskStore(live_pool).active_task()
    assert active is None, "archived done task must not appear as active"

    from sali.runtime.attention import attention_snapshot
    snap = await attention_snapshot(live_pool)
    assert snap["has_primary"] is False


@pytest.mark.asyncio
async def test_archived_task_is_queryable_by_id_for_followup(live_pool) -> None:
    """The whole point of Turn 1: §15 follow-up detection needs to be able to look up a
    completed task by id and see its objective + workspace_root."""
    await _clear(live_pool)
    task_id = await _seed_completed_task_with_children(live_pool)
    await TaskStore(live_pool)._archive_and_cleanup(task_id)

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT objective, status, workspace_root, archived_at "
            "FROM sali.task WHERE id = $1", task_id)
    assert row is not None
    assert row["objective"] == "Ship the metrics dashboard"
    assert row["status"] == "done"
    assert row["archived_at"] is not None


@pytest.mark.asyncio
async def test_recent_completed_tasks_query_finds_archived_row(live_pool) -> None:
    """A future follow-up-detector will need to enumerate 'tasks Sali finished recently.'
    Proves the retire-not-delete path exposes exactly that."""
    await _clear(live_pool)
    for _ in range(3):
        await _seed_completed_task_with_children(live_pool)
    async with live_pool.acquire() as conn:
        # A representative shape for the future follow-up detector query.
        rows = await conn.fetch(
            "SELECT id, objective, completed_at FROM sali.task "
            "WHERE status = 'done' ORDER BY completed_at DESC LIMIT 10")
    assert len(rows) == 3
    # Now archive them all and confirm they still surface with archived_at set.
    for r in rows:
        await TaskStore(live_pool)._archive_and_cleanup(r["id"])
    async with live_pool.acquire() as conn:
        after = await conn.fetch(
            "SELECT id, archived_at FROM sali.task "
            "WHERE status='done' AND archived_at IS NOT NULL")
    assert len(after) == 3, "recent-completed query must still find all three after archive"
