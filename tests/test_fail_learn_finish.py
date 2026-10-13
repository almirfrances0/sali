"""He failed, he learned, he came back and finished it.

The chain: a step fails -> queue_gaps files a `blocked_step` gap carrying task_id+step_seq ->
research_pass answers it -> _unblock_step reopens that exact step WITH the lesson, without erasing
what was already tried.

The guards are the interesting half. This path REOPENS WORK, so it is one wrong predicate away from
resurrecting something Almir killed — which has actually happened here before (a task he stopped came
back five hours later and ran for six). Both the enqueue side and the reopen side used to test for
task status 'completed', a word this schema does not contain, so both admitted done, failed AND
ABANDONED tasks.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from sali.learning import queue
from sali.learning.research import _unblock_step


async def _task_with_failed_step(conn, *, status: str = "running", error: str = "ffmpeg: command not found"):
    """A live task whose step 2 has genuinely failed."""
    task_id = uuid4()
    await conn.execute(
        "INSERT INTO task (id, objective, status) VALUES ($1, $2, $3)",
        task_id, "convert the recordings to mp4", status)
    await conn.execute(
        "INSERT INTO task_step (task_id, seq, description, status, last_error, failure_class, "
        "  attempts, finished_at) "
        "VALUES ($1, 2, 'convert the files', 'failed', $2, 'recoverable', 2, now())",
        task_id, error)
    return task_id


@pytest.mark.asyncio
async def test_b_a_stuck_step_becomes_a_learning_gap_bound_to_the_work(live_pool) -> None:
    async with live_pool.acquire() as conn:
        task_id = await _task_with_failed_step(conn)
        assert await queue.queue_gaps(conn) >= 1

        items = await queue.pending(conn)
        blocked = [i for i in items if i.kind == "blocked_step"]
        assert blocked, "a failed step on a live task must become a gap"
        item = blocked[0]
        assert item.task_id == task_id, "the gap must know WHICH work it is blocking"
        assert item.step_seq == 2
        assert "ffmpeg" in item.subject, "the error is what gets researched"
        # A gap holding up real work outranks idle curiosity.
        assert items[0].kind == "blocked_step"


@pytest.mark.asyncio
async def test_b_the_lesson_reopens_the_step_without_amnesia(live_pool) -> None:
    async with live_pool.acquire() as conn:
        task_id = await _task_with_failed_step(conn)
        await queue.queue_gaps(conn)
        item = [i for i in await queue.pending(conn) if i.kind == "blocked_step"][0]

        assert await _unblock_step(conn, item, "install ffmpeg first: sudo apt install ffmpeg") is True

        row = await conn.fetchrow(
            "SELECT status, note, attempts, last_error FROM task_step "
            "WHERE task_id=$1 AND seq=2", task_id)
        assert row["status"] == "pending", "the step must be workable again"
        assert "install ffmpeg first" in row["note"], "and it must carry what was learned"
        # Deliberately NOT cleared: the retry driver reads these to decide whether another attempt is
        # even warranted, and they are what stop Sali walking back into the same wall.
        assert row["attempts"] == 2
        assert "ffmpeg" in row["last_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["abandoned", "cancelled", "done", "failed", "superseded"])
async def test_b_dead_work_is_never_reopened(live_pool, status: str) -> None:
    """THE guard. 'abandoned' is work Almir stopped; reopening it is the resurrection bug.

    It got through because both predicates excluded 'completed' — not a status this schema has —
    instead of the real terminal set."""
    async with live_pool.acquire() as conn:
        task_id = await _task_with_failed_step(conn, status=status)
        await queue.queue_gaps(conn)
        item = queue.LearningItem(id=uuid4(), kind="blocked_step", subject="x", reason=None,
                                  priority=1, task_id=task_id, step_seq=2)

        assert await _unblock_step(conn, item, "a lesson") is False, \
            f"a step on a {status} task must never be reopened"
        assert await conn.fetchval(
            "SELECT status FROM task_step WHERE task_id=$1 AND seq=2", task_id) == "failed"
        # ...and it should never have been queued for research in the first place.
        queued = await conn.fetchval(
            "SELECT count(*) FROM learning_queue WHERE task_id=$1", task_id)
        assert queued == 0, f"a {status} task's step must not even be queued"


@pytest.mark.asyncio
async def test_b_a_revoked_task_is_never_reopened(live_pool) -> None:
    """Revocation leaves a tombstone that outlives the task row's status. Honour it on both sides."""
    async with live_pool.acquire() as conn:
        task_id = await _task_with_failed_step(conn)
        await conn.execute(
            "INSERT INTO revoked_intent (id, task_id, objective, reason, revoked_by) "
            "VALUES ($1,$2,'convert the recordings','almir said stop','almir')",
            uuid4(), task_id)

        await queue.queue_gaps(conn)
        assert await conn.fetchval(
            "SELECT count(*) FROM learning_queue WHERE task_id=$1", task_id) == 0

        item = queue.LearningItem(id=uuid4(), kind="blocked_step", subject="x", reason=None,
                                  priority=1, task_id=task_id, step_seq=2)
        assert await _unblock_step(conn, item, "a lesson") is False


@pytest.mark.asyncio
async def test_b_reopening_is_bounded(live_pool) -> None:
    """A lesson that does not actually unstick the step must not reopen it forever. After three
    goes it stays failed and stays visible — which is the honest state, not a hidden retry loop."""
    async with live_pool.acquire() as conn:
        task_id = await _task_with_failed_step(conn)
        item = queue.LearningItem(id=uuid4(), kind="blocked_step", subject="x", reason=None,
                                  priority=1, task_id=task_id, step_seq=2)
        for attempt in range(3):
            assert await _unblock_step(conn, item, f"lesson {attempt}") is True
            await conn.execute(
                "UPDATE task_step SET status='failed', finished_at=now() "
                "WHERE task_id=$1 AND seq=2", task_id)   # it failed again
        assert await _unblock_step(conn, item, "lesson 4") is False, "must stop after three"
        assert await conn.fetchval(
            "SELECT status FROM task_step WHERE task_id=$1 AND seq=2", task_id) == "failed"
