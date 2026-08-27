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


async def test_a_substantial_run_produces_a_lesson(db_conn: Any) -> None:
    await _run(db_conn, "deploy the app", tools=4)
    provider = _lesson("Always free port 3000 before starting the dev server.")

    kept = await reflect_on_recent(db_conn, provider, min_tools=3)
    assert kept == 1
    row = await db_conn.fetchrow(
        "SELECT content, structured FROM memory WHERE structured->>'kind'='reflection'")
    assert row is not None and "port 3000" in row["content"]
    assert row["structured"]["certainty"] == "learned"
    # a second pass does NOT re-reflect the same run
    assert await reflect_on_recent(db_conn, provider, min_tools=3) == 0


async def test_trivial_runs_are_not_reflected_on(db_conn: Any) -> None:
    await _run(db_conn, "hi", tools=1)  # below the tool threshold
    assert await reflect_on_recent(db_conn, _lesson("x"), min_tools=3) == 0
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE structured->>'kind'='reflection'") == 0


async def test_none_keeps_nothing_but_marks_the_run(db_conn: Any) -> None:
    run_id = await _run(db_conn, "listed some files", tools=3)
    kept = await reflect_on_recent(db_conn, _lesson("none"), min_tools=3)
    assert kept == 0
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE structured->>'kind'='reflection'") == 0
    # still marked reflected, so it isn't retried forever
    marked = await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='learning.reflected' AND subject_id=$1", run_id)
    assert marked == 1
