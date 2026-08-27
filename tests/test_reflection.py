"""Phase 3 · Increment 12 — self-reflection (§57).

After a substantial task, Sali keeps the single reusable lesson as memory — throttled, gated to
non-trivial runs, each run reflected exactly once, 'none' kept nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.learning.reflection import reflect_on_recent
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


async def _run(conn: Any, user_input: str, *, tools: int) -> Any:
    run_id = await conn.fetchval(
        "INSERT INTO agent_runs (run_id, session_id, user_input, state, status) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), $1, 'done', 'completed') RETURNING run_id",
        user_input)
    for i in range(tools):
        await conn.execute(
            "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
            "VALUES ($1,$2,'{}',true,'observed')", run_id, f"tool_{i}")
    return run_id


def _lesson(text: str) -> FakeModelProvider:
    return FakeModelProvider(responses=[ChatResult(text, None, [], 3, 3, "fake")])


async def test_a_substantial_run_produces_a_structured_experience(db_conn: Any) -> None:
    await _run(db_conn, "deploy the app", tools=4)
    provider = _lesson("Always free port 3000 before starting the dev server.")

    kept = await reflect_on_recent(db_conn, provider, min_tools=3)
    assert kept == 1
    row = await db_conn.fetchrow(
        "SELECT content, layer, structured FROM memory WHERE structured->>'kind'='experience'")
    assert row is not None and row["layer"] == "episodic"           # ONE structured experience (§36)
    assert "port 3000" in row["structured"]["lesson"]
    assert row["structured"]["task"] == "deploy the app"
    assert row["structured"]["outcome"] == "success" and row["structured"]["certainty"] == "learned"
    # no separate bare "Lesson learned" semantic is emitted (the near-duplicate is gone)
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE content LIKE 'Lesson learned:%'") == 0
    # a second pass does NOT re-reflect the same run
    assert await reflect_on_recent(db_conn, provider, min_tools=3) == 0


async def test_trivial_runs_are_not_reflected_on(db_conn: Any) -> None:
    await _run(db_conn, "hi", tools=1)  # below the tool threshold
    assert await reflect_on_recent(db_conn, _lesson("x"), min_tools=3) == 0
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE structured->>'kind'='experience'") == 0


async def test_clean_run_with_no_lesson_keeps_nothing_but_marks_it(db_conn: Any) -> None:
    run_id = await _run(db_conn, "listed some files", tools=3)  # all succeed, no errors
    kept = await reflect_on_recent(db_conn, _lesson("none"), min_tools=3)
    assert kept == 0
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE structured->>'kind'='experience'") == 0
    # still marked reflected, so it isn't retried forever
    marked = await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='learning.reflected' AND subject_id=$1", run_id)
    assert marked == 1
