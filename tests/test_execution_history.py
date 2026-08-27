"""Memory correctness · Increment 2 — authoritative execution history (§2).

The runtime knows the exact order every tool ran; the model never has to infer it. tool_history returns
deterministic records with a per-run sequence number, sanitized args + stable hash, timing, and result.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.tools.builtins.history_tool import ToolHistory, execution_record
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


def test_execution_record_is_deterministic() -> None:
    from datetime import UTC, datetime

    t = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    row = {"id": uuid4(), "run_id": uuid4(), "session_id": uuid4(), "seq": 2,
           "tool_name": "execute_command", "plan": {"args": {"command": "ls -la"}},
           "observed": {"display": "exit 0"}, "status": "verified_success", "success": True,
           "error": None, "started_at": t, "finished_at": t}
    rec = execution_record(row)
    assert rec["sequence_number"] == 2 and rec["tool_name"] == "execute_command"
    assert rec["sanitized_arguments"] == {"command": "ls -la"}
    assert len(rec["arguments_hash"]) == 16 and rec["result_summary"] == "exit 0"
    # the hash is stable for the same args
    assert execution_record(row)["arguments_hash"] == rec["arguments_hash"]


async def _exec(conn: Any, run_id: Any, tool: str, seq_hint: str) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, status, started_at) "
        "VALUES ($1,$2,$3,true,'observed', now() + ($4 || ' seconds')::interval)",
        run_id, tool, {"args": {"n": seq_hint}}, seq_hint)


async def test_tool_history_returns_authoritative_order(live_pool: Any) -> None:
    run = uuid4()
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_runs (run_id, session_id, user_input, state, status) "
            "VALUES ($1, gen_random_uuid(), 'x', 'done', 'completed')", run)
        # insert out of order; the runtime's started_at defines the true sequence
        await _exec(conn, run, "third", "3")
        await _exec(conn, run, "first", "1")
        await _exec(conn, run, "second", "2")

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool)
    res = await ToolHistory().run({"run_id": str(run)}, ctx)
    assert res.ok
    execs = res.output["executions"]
    # sequence_number reflects the true started_at order regardless of insert order
    by_seq = {e["tool_name"]: e["sequence_number"] for e in execs}
    assert by_seq == {"first": 1, "second": 2, "third": 3}
    assert all(e["session_id"] for e in execs)  # session joined from agent_runs


async def test_tool_history_needs_a_datastore() -> None:
    res = await ToolHistory().run({}, ToolContext(settings=Settings(), clock=SystemClock()))
    assert not res.ok and "isn't available" in (res.error or "")
