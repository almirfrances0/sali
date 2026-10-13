"""Phase 3 · Increment 10 — the environmental timeline (§62).

"What happened today?" answered from the durable event log: meaningful events rendered
deterministically into a readable timeline, internal bookkeeping omitted, immediate repeats collapsed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.tools.builtins.timeline_tool import Timeline, build_entries, summarize
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db

_T = datetime(2026, 8, 27, 9, 30, 0, tzinfo=UTC)


def test_summarize_keeps_meaningful_omits_bookkeeping() -> None:
    assert summarize("conversation.turn", {}) == "talked with Almir"
    assert summarize("sali.proactive", {"message": "nginx failed"}) == "flagged: nginx failed"
    assert summarize("twin.entity_added", {"key": "software:docker"}) == "noticed new: software:docker"
    # a low-tier desktop observation is NOT timeline-worthy
    assert summarize("desktop.observed", {"summary": "x", "tier": "interesting"}) is None
    assert summarize("desktop.observed", {"summary": "nginx failed", "tier": "critical"}) == "nginx failed"
    # internal bookkeeping is omitted
    assert summarize("tool.intel_pass", {}) is None
    assert summarize("memory.observed", {}) is None


def test_build_entries_collapses_repeats() -> None:
    rows = [
        {"event_type": "conversation.turn", "payload": {}, "created_at": _T},
        {"event_type": "conversation.turn", "payload": {}, "created_at": _T},  # immediate repeat
        {"event_type": "sali.proactive", "payload": {"message": "disk full"}, "created_at": _T},
        {"event_type": "tool.intel_pass", "payload": {}, "created_at": _T},    # omitted
    ]
    entries = build_entries(rows)
    assert [e["summary"] for e in entries] == ["talked with Almir", "flagged: disk full"]
    assert entries[0]["at"] == "09:30"


async def _emit(conn: Any, event_type: str, payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES ($1,'desktop',$2)",
        event_type, payload)


async def test_timeline_tool_reads_the_event_log(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await _emit(conn, "sali.proactive", {"message": "a new service appeared"})
        await _emit(conn, "desktop.observed", {"summary": "3 changes in project-x", "tier": "important"})
        await _emit(conn, "tool.intel_pass", {"discovered": 2400})  # noise → omitted

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool)
    res = await Timeline().run({"hours": 1}, ctx)
    assert res.ok
    summaries = [e["summary"] for e in res.output["entries"]]
    assert "flagged: a new service appeared" in summaries
    assert "3 changes in project-x" in summaries
    assert all("intel_pass" not in s for s in summaries)


async def test_timeline_tool_needs_a_datastore() -> None:
    ctx = ToolContext(settings=Settings(), clock=SystemClock())  # no pool
    res = await Timeline().run({}, ctx)
    assert not res.ok and "isn't available" in (res.error or "")
