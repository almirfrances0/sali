"""Phase 2 · Increment 8 — the learning queue + bounded curiosity (§45/§46/§79).

Sali records what it doesn't understand as a durable agenda, deduped, worked off under a budget so
curiosity never becomes infinite autonomous experimentation. Populated deterministically from
attention's investigate signals and recurring failures.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.learning.queue import (
    Budget,
    count_pending,
    drop,
    enqueue,
    pending,
    queue_gaps,
    resolve,
)

pytestmark = pytest.mark.db


async def test_enqueue_dedups_pending_items(db_conn: Any) -> None:
    assert await enqueue(db_conn, kind="investigate", subject="new service on :8080") is True
    assert await enqueue(db_conn, kind="investigate", subject="new service on :8080") is False  # dup
    assert await count_pending(db_conn) == 1


async def test_pending_is_ordered_by_priority(db_conn: Any) -> None:
    await enqueue(db_conn, kind="investigate", subject="low", priority=7)
    await enqueue(db_conn, kind="recurring_failure", subject="high", priority=1)
    items = await pending(db_conn)
    assert [i.subject for i in items] == ["high", "low"]


async def test_resolve_and_drop_close_items(db_conn: Any) -> None:
    await enqueue(db_conn, kind="unknown_tool", subject="frobnicate")
    item = (await pending(db_conn))[0]
    await resolve(db_conn, item.id, outcome="it's a JSON linter")
    assert await count_pending(db_conn) == 0
    # a resolved subject can be queued again (it recurred) — dedup only blocks PENDING duplicates
    assert await enqueue(db_conn, kind="unknown_tool", subject="frobnicate") is True
    again = (await pending(db_conn))[0]
    await drop(db_conn, again.id)
    assert await count_pending(db_conn) == 0


async def _observed_investigate(conn: Any, summary: str) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES ('desktop.observed','desktop',$1)",
        {"kind": "port_opened", "summary": summary, "action": "investigate"})


async def _failed(conn: Any, command: str) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
        "VALUES ($1,'execute_command',$2,false,'observed')",
        uuid4(), {"args": {"command": command}})


async def test_queue_gaps_notices_investigate_and_recurring_failures(db_conn: Any) -> None:
    await _observed_investigate(db_conn, "new service on tcp:0.0.0.0:9000")
    for _ in range(3):  # a command that keeps failing (>= 3 times) is a knowledge gap
        await _failed(db_conn, "docker compose up")

    added = await queue_gaps(db_conn, budget=Budget(max_new_per_pass=5))
    assert added == 2
    subjects = {i.subject for i in await pending(db_conn)}
    assert any("tcp:0.0.0.0:9000" in s for s in subjects)
    assert "docker compose up" in subjects


async def test_curiosity_is_bounded_by_budget(db_conn: Any) -> None:
    for i in range(6):
        await _observed_investigate(db_conn, f"new service {i}")
    added = await queue_gaps(db_conn, budget=Budget(max_new_per_pass=2))
    assert added == 2  # never floods — bounded curiosity (§46/§79)
    assert await count_pending(db_conn) == 2


async def test_self_state_surfaces_the_learning_queue(live_pool: Any) -> None:
    from sali.runtime.self_state import SelfStateStore

    async with live_pool.acquire() as conn:
        await enqueue(conn, kind="investigate", subject="new listening service", priority=2)
    view = await SelfStateStore(live_pool).assemble()
    assert any("new listening service" in q for q in view["learning_queue"])
