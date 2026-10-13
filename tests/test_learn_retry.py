"""fail → notice → learn → COME BACK AND FINISH (§18).

Sali could already fail, and could already research. What did not exist was the return leg: the
learning queue had no reference to the work a gap was blocking, so an answer he went and found could
never be handed back to the step that was waiting for it. He learned, and left.

These tests lock the whole chain, including the two things that make a retry INFORMED rather than
amnesiac: the lesson is attached to the step, and the attempt history survives it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from sali.learning import failures, queue, research

pytestmark = pytest.mark.db


class _Provider:
    """Distils what a real model would, so these assertions are about the wiring, not the model."""

    async def chat(self, messages: Any, options: Any = None, **kw: Any) -> Any:
        return SimpleNamespace(content="ping is not installed here; use `nc -z host port` instead.")


async def _search(query: str) -> list[dict[str, str]]:
    return [{"snippet": "iputils-ping is absent from the base image", "url": "https://example.org/p"}]


async def _online() -> bool:
    return True


async def _seed_stuck_step(c: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """A live task whose step 3 died on a real error, with the tool execution that killed it."""
    task_id, exec_id = uuid.uuid4(), uuid.uuid4()
    await c.execute(
        "INSERT INTO task (id, objective, status, created_at, updated_at) "
        "VALUES ($1, 'confirm the router answers', 'running', now(), now())", task_id)
    await c.execute(
        "INSERT INTO task_step (task_id, seq, description, status, attempts, last_error, "
        "  failure_class, finished_at, note) VALUES ($1, 3, 'ping the router', 'failed', 2, "
        "  'bash: line 1: ping: command not found', 'recoverable', now(), 'first attempt')", task_id)
    await c.execute(
        "INSERT INTO tool_execution (id, run_id, run_kind, tool_name, status, success, error, plan, "
        "  started_at) VALUES ($1, $2, 'task', 'execute_command', 'verified_failure', false, "
        "  'bash: line 1: ping: command not found', "
        "  '{\"args\": {\"command\": \"ping -c1 192.168.1.1\"}}'::jsonb, now())", exec_id, uuid.uuid4())
    await c.execute(
        "INSERT INTO task_execution (task_id, step_seq, tool_name, execution_id, status) "
        "VALUES ($1, 3, 'execute_command', $2, 'failed')", task_id, exec_id)
    return task_id, exec_id


async def test_a_stuck_step_becomes_a_learning_gap_that_knows_its_task(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        task_id, _ = await _seed_stuck_step(c)

        assert await queue.queue_gaps(c) >= 1
        row = await c.fetchrow(
            "SELECT subject, priority, task_id, step_seq FROM learning_queue WHERE kind='blocked_step'")
        # ONE failure is enough when it stopped real work — the recurring-failure source needs three,
        # which is why a task could sit blocked forever without Sali ever noticing there was anything
        # to learn.
        assert row is not None
        assert row["task_id"] == task_id and row["step_seq"] == 3
        assert row["priority"] == 1  # blocked work outranks idle curiosity
        assert "command not found" in row["subject"]


async def test_blocked_work_is_researched_before_idle_curiosity(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        await _seed_stuck_step(c)
        await queue.queue_gaps(c)
        await queue.enqueue(c, kind="curiosity", subject="what is a CNI plugin", priority=1)

        items = await queue.pending(c, limit=5)
        assert items[0].kind == "blocked_step"
        assert items[0].task_id is not None  # the link survives the read, not just the write


async def test_learning_reopens_the_step_without_erasing_its_history(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        task_id, _ = await _seed_stuck_step(c)
        await queue.queue_gaps(c)

    assert await research.research_pass(
        live_pool, _Provider(), search=_search, online=_online) >= 1

    async with live_pool.acquire() as c:
        step = await c.fetchrow(
            "SELECT status, attempts, last_error, failure_class, note FROM task_step "
            "WHERE task_id=$1 AND seq=3", task_id)
        assert step["status"] == "pending"          # runnable again — this is the return leg
        assert "learned:" in (step["note"] or "")   # and it retries KNOWING what it learned

        # The retry policy reads attempts/failure_class to decide whether another attempt is even
        # warranted, and the engine shows Sali "you already tried this N times". Clearing them would
        # turn an informed retry into an amnesiac one walking back into the same wall.
        assert step["attempts"] == 2
        assert "command not found" in step["last_error"]
        assert step["failure_class"] == "recoverable"

        assert await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='task.step_unblocked'") == 1
        assert await c.fetchval(
            "SELECT count(*) FROM memory WHERE structured->>'kind'='researched'") >= 1


async def test_a_finished_task_is_never_resurrected_by_a_late_lesson(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        task_id, _ = await _seed_stuck_step(c)
        await queue.queue_gaps(c)
        # Almir cancelled it while the research was still running.
        await c.execute("UPDATE task SET status='cancelled' WHERE id=$1", task_id)

    await research.research_pass(live_pool, _Provider(), search=_search, online=_online)

    async with live_pool.acquire() as c:
        # The lesson is still learned and stored; only the dead work stays dead.
        assert await c.fetchval(
            "SELECT status FROM task_step WHERE task_id=$1 AND seq=3", task_id) == "failed"
        assert await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='task.step_unblocked'") == 0
        assert await c.fetchval(
            "SELECT status FROM learning_queue WHERE kind='blocked_step'") == "resolved"


async def test_a_failure_that_blocked_work_leaves_an_open_incident(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        task_id, _ = await _seed_stuck_step(c)

        assert await failures.record_failures(c) == 1
        inc = await c.fetchrow(
            "SELECT content, structured FROM memory WHERE structured->>'kind'='incident'")
        assert inc is not None  # a bare failure used to be dropped, leaving nothing to close
        st = inc["structured"]
        if isinstance(st, str):
            import json
            st = json.loads(st)
        assert st["outcome"] == "open"
        # An open incident asserts no cause and no fix — inventing either is confabulation (§25).
        assert st["correction"] is None and st["verified"] is False
        assert st["task_id"] == str(task_id) and st["step_seq"] == 3


async def test_a_stray_failure_that_blocked_nothing_is_still_noise(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO tool_execution (id, run_id, run_kind, tool_name, status, success, error, "
            "  plan, started_at) VALUES ($1, $2, 'chat', 'execute_command', 'verified_failure', "
            "  false, 'transient blip', '{\"args\": {\"command\": \"ls /nope\"}}'::jsonb, now())",
            uuid.uuid4(), uuid.uuid4())

        assert await failures.record_failures(c) == 0
        assert await c.fetchval(
            "SELECT count(*) FROM memory WHERE structured->>'kind'='incident'") == 0
