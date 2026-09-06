"""Reflection produces LESSONS, not audit copies of the task.

Prompt A memory audit: "task history != memory." The previous writer stored
`"Experience — {user_input}: {headline}"` as the content of every reflected run, which turned every
completed background task into a keyword-rich noise memory. Live proof from the DB before this fix:
31 rows shaped `Experience — TASK: ...` accumulated 370 reads (mean 11.94 each) — more per row than
useful memories — and dominated retrieval on any task-related query.

These tests pin the corrected contract at the writer boundary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from sali.core.enums import MemoryLayer
from sali.learning.reflection import reflect_on_recent

pytestmark = pytest.mark.db


class _Provider:
    """Test double that returns a fixed lesson (or 'none') for the reflection call."""

    def __init__(self, lesson: str) -> None:
        self._lesson = lesson
        self.chat = AsyncMock(side_effect=self._reply)

    async def _reply(self, *args: Any, **kwargs: Any) -> Any:
        class _R:
            content = self._lesson
        r = _R()
        r.content = self._lesson
        return r


async def _seed_run(db_conn: Any, user_input: str, n_tools: int = 3) -> str:
    """Create a completed agent_run with N tool executions, ready to be reflected on."""
    from uuid import uuid4
    run_id = uuid4()
    session_id = uuid4()
    await db_conn.execute(
        "INSERT INTO agent_runs (run_id, session_id, user_input, state, status, iteration) "
        "VALUES ($1,$2,$3,'DONE','completed', 1)", run_id, session_id, user_input)
    for i in range(n_tools):
        await db_conn.execute(
            "INSERT INTO tool_execution (run_id, tool_name, status, success, plan) "
            "VALUES ($1,'execute_command','verified_success',true,$2)",
            run_id, {"args": {"command": f"echo {i}"}})
    return str(run_id)


async def test_a_background_verification_run_is_never_reflected(db_conn: Any) -> None:
    """The bug that produced 31 noise memories. Internal task-verification runs whose input starts
    with `TASK:` must be skipped by the reflection selector — the tool_execution rows and task
    record already preserve what happened, and turning the raw internal prompt into a memory made
    it keyword-searchable in a way that polluted retrieval."""
    await _seed_run(db_conn, "TASK: Verify stored procedure for creating a Python archive script")
    provider = _Provider("some lesson")
    wrote = await reflect_on_recent(db_conn, provider, min_tools=3)
    assert wrote == 0, "background verification runs must not be reflected on"
    assert not provider.chat.called, "the selector must skip before the model is asked"


async def test_a_grounding_check_run_is_never_reflected(db_conn: Any) -> None:
    """Same shape, different marker: the grounding faculty's own housekeeping prompts."""
    await _seed_run(db_conn, "[my own background check] verify that /home/almir/Desktop exists")
    wrote = await reflect_on_recent(db_conn, _Provider("some lesson"))
    assert wrote == 0


async def test_a_run_with_no_lesson_writes_no_memory(db_conn: Any) -> None:
    """The old writer emitted a memory for any run with a lesson OR any errors. Silence — no lesson,
    no memory. The audit trail lives in agent_runs + tool_execution; it doesn't need to double as a
    keyword-searchable semantic entry."""
    await _seed_run(db_conn, "help me tidy up the archive folder")
    wrote = await reflect_on_recent(db_conn, _Provider("none"))
    assert wrote == 0
    row = await db_conn.fetchval("SELECT count(*) FROM memory WHERE layer='episodic'")
    assert row == 0


async def test_when_there_IS_a_lesson_the_memory_content_IS_the_lesson(db_conn: Any) -> None:
    """The whole point of the fix: content is the lesson, not `Experience — {user_input}: ...`. So a
    later retrieval for "how did X work last time" finds the lesson itself instead of a copy of the
    original task prompt."""
    await _seed_run(db_conn, "help me tidy up the archive folder")
    lesson = "Prefer atomic mv over cp-then-rm when tidying archives, so a mid-run failure leaves no partial state."
    wrote = await reflect_on_recent(db_conn, _Provider(lesson))
    assert wrote == 1
    row = await db_conn.fetchrow(
        "SELECT content, structured FROM memory WHERE layer='episodic' AND valid_until IS NULL")
    assert row is not None
    # The reflection normaliser strips trailing punctuation from the model output.
    assert row["content"] == lesson.rstrip(".").rstrip(), "content is the lesson (normalised)"
    assert row["structured"]["task"].startswith("help me tidy"), "structured keeps the task context"
    # The scaffolding shape that produced the noise cluster must not reappear.
    assert not row["content"].startswith("Experience —"), \
        "the noise-generating shape is retired for good"


async def test_a_run_is_marked_reflected_exactly_once(db_conn: Any) -> None:
    """Regardless of whether a memory was written, the event bookmark must land so the same run
    isn't picked again on the next pass — otherwise the reflector loops on the newest run forever."""
    run_id = await _seed_run(db_conn, "run me through the last docker deploy")
    await reflect_on_recent(db_conn, _Provider("none"))
    marker = await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='learning.reflected' AND subject_id=$1",
        run_id)
    assert marker == 1
