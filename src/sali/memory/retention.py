"""Retention / garbage collection (spec §51/§62).

The store must not become "an unmaintainable database of garbage" — but history is NOT deleted
recklessly. The durable record is permanent by design: memories and graph facts decay in RANK but
are never removed (§28), and the ``event`` log is append-only (a DB trigger forbids DELETE — it's
Sali's audit spine). The one thing that grows fast with no lasting value is the per-run FSM JOURNAL
(``run_events``) — the blow-by-blow of every finished turn, useful for a live/recent run and then
just operational noise. GC prunes only that, and only for runs finished well past a retention window.
The run SUMMARY (``agent_runs``) and everything durable are untouched.
"""

from __future__ import annotations

import contextlib
from typing import Any


async def gc(conn: Any, *, run_journal_retention_days: int = 90) -> int:
    """Prune high-volume, low-lasting-value operational history of old, FINISHED runs (§51, Final audit §41).
    Never touches memory, graph, contradictions, the event log, experiences, learning candidates, or the run
    summaries. Returns rows reclaimed. Caller owns the transaction.

    Covers three per-turn/per-tool-call tables that would otherwise grow without bound on a years-long agent:
    the FSM journal (``run_events``), the tool-call audit projection (``tool_audit``), and the executed-tool
    detail (``tool_execution``) — all only for runs finished well past the retention window. The durable
    task archive (sali-works/tasks/) and the append-only ``event`` spine keep the permanent record."""
    finished = ("SELECT run_id FROM agent_runs WHERE status IN ('completed','aborted','failed') "
                "AND updated_at < now() - make_interval(days => $1)")
    reclaimed = 0
    for table in ("run_events", "tool_audit", "tool_execution"):
        result = await conn.execute(
            f"DELETE FROM {table} WHERE run_id IN ({finished})", run_journal_retention_days)
        with contextlib.suppress(ValueError, IndexError, AttributeError):
            reclaimed += int(result.split()[-1])  # asyncpg returns e.g. "DELETE 42"
    return reclaimed
