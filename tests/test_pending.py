"""PendingAction continuity (§2/§6/§10): a proposed command lives as a durable 'planned' tool_execution
row, the latest proposal wins, and older proposals are superseded."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.runtime import pending

pytestmark = pytest.mark.db


async def _run(conn: Any, session: Any) -> Any:
    run_id = uuid4()
    await conn.execute(
        "INSERT INTO agent_runs (run_id, session_id, user_input, state, status) "
        "VALUES ($1, $2, 'x', 'respond', 'completed')",
        run_id, session)
    return run_id


async def test_capture_writes_planned_row_and_latest_pending_reads_it(live_pool: Any) -> None:
    session = uuid4()
    async with live_pool.acquire() as c:
        run_id = await _run(c, session)
        eid = await pending.capture(
            c, run_id, "execute_command", {"command": "sudo apt install php-xml"},
            description="Install php-xml", risk_level=1)
        pa = await pending.latest_pending(c, session)
        status = await c.fetchval("SELECT status FROM tool_execution WHERE id=$1", eid)

    assert status == "planned"  # a proposed-but-unrun command leaves a durable row (§6 honesty)
    assert pa is not None and pa.exec_id == eid
    assert pa.tool_name == "execute_command" and pa.command == "sudo apt install php-xml"
    assert pa.description == "Install php-xml"


async def test_newer_proposal_supersedes_older(live_pool: Any) -> None:
    session = uuid4()
    async with live_pool.acquire() as c:
        run_id = await _run(c, session)
        e1 = await pending.capture(c, run_id, "execute_command", {"command": "echo one"})
        e2 = await pending.capture(c, run_id, "execute_command", {"command": "echo two"})
        pa = await pending.latest_pending(c, session)
        s1 = await c.fetchval("SELECT status FROM tool_execution WHERE id=$1", e1)

    assert pa is not None and pa.exec_id == e2 and pa.command == "echo two"  # most recent wins (§10)
    assert s1 == "aborted"  # the earlier proposal was superseded, never left ambiguous


async def test_no_pending_for_a_different_session(live_pool: Any) -> None:
    session, other = uuid4(), uuid4()
    async with live_pool.acquire() as c:
        run_id = await _run(c, session)
        await pending.capture(c, run_id, "execute_command", {"command": "echo hi"})
        assert await pending.latest_pending(c, other) is None  # scoped to the session
