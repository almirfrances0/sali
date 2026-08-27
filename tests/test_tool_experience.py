"""Tool Intelligence — Increment 5: per-binary tool-experience mining (§16/§46/§55/§67).

Aggregates how each underlying binary actually behaved (reliability/latency/failure-modes) from the
durable tool_execution record into a tool:<binary> memory — evidence-gated, refreshed without churn.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.learning.tool_experience import binary_of, learn_tool_experiences

pytestmark = pytest.mark.db


def test_binary_of_extracts_the_real_program() -> None:
    assert binary_of("nmap -sV 10.0.0.1") == "nmap"
    assert binary_of("sudo nmap -sV host") == "nmap"          # wrapper skipped
    assert binary_of("FOO=1 /usr/bin/jq . data.json") == "jq"  # env-assign + path stripped
    assert binary_of("time nice ffmpeg -i a.mp4 b.mkv") == "ffmpeg"
    assert binary_of("grep needle | sort") == "grep"
    assert binary_of("") == ""


async def _exec_row(conn: Any, command: str, *, success: bool, duration_ms: int = 100,
                    error: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, duration_ms, error, status) "
        "VALUES ($1, 'execute_command', $2, $3, $4, $5, 'observed')",
        uuid4(), {"args": {"command": command}}, success, duration_ms, error)


async def test_experience_is_mined_after_enough_runs(db_conn: Any) -> None:
    await _exec_row(db_conn, "jq . a.json", success=True, duration_ms=50)
    await _exec_row(db_conn, "jq -r .name b.json", success=True, duration_ms=70)
    await _exec_row(db_conn, "jq .broken c.json", success=False, duration_ms=30,
                    error="jq: error: syntax error")

    learned = await learn_tool_experiences(db_conn, threshold=3)

    assert len(learned) == 1 and learned[0].binary == "jq"
    assert learned[0].runs == 3 and learned[0].successes == 2 and learned[0].failures == 1
    assert learned[0].reliability == round(2 / 3, 3)
    row = await db_conn.fetchrow(
        "SELECT content, structured, layer FROM memory WHERE claim_key='tool:jq' AND valid_until IS NULL")
    assert row is not None and row["layer"] == "semantic"
    assert row["structured"]["certainty"] == "learned"
    assert row["structured"]["latency_p50_ms"] == 50  # median of 50/70/30 (sorted 30,50,70)
    assert "jq: error: syntax error" in row["structured"]["failure_modes"]


async def test_below_threshold_is_not_characterised(db_conn: Any) -> None:
    await _exec_row(db_conn, "rare-tool x", success=True)
    learned = await learn_tool_experiences(db_conn, threshold=3)
    assert learned == []
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE claim_key='tool:rare-tool'") == 0


async def test_refresh_updates_stats_without_churn(db_conn: Any) -> None:
    for _ in range(3):
        await _exec_row(db_conn, "curl https://x", success=True)
    first = await learn_tool_experiences(db_conn, threshold=3)
    assert first[0].runs == 3

    # more runs happen, then re-mine — the SAME memory row is refreshed, not a new/contradicting one
    for _ in range(2):
        await _exec_row(db_conn, "curl https://y", success=False, error="curl: (7) refused")
    second = await learn_tool_experiences(db_conn, threshold=3)

    assert second[0].runs == 5 and second[0].failures == 2
    rows = await db_conn.fetch("SELECT id FROM memory WHERE claim_key='tool:curl'")
    assert len(rows) == 1  # single evolving claim — no supersession churn
