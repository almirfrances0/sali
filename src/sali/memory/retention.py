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

from typing import Any


async def gc(conn: Any, *, run_journal_retention_days: int = 90) -> int:
    """Prune the FSM journal of old, FINISHED runs (§51). Never touches memory, graph, contradictions,
    the event log, or the run summaries. Returns rows reclaimed. Caller owns the transaction."""
    result = await conn.execute(
        "DELETE FROM run_events WHERE run_id IN ("
        "  SELECT run_id FROM agent_runs "
        "  WHERE status IN ('completed','aborted','failed') "
        "    AND updated_at < now() - make_interval(days => $1))",
        run_journal_retention_days,
    )
    try:
        return int(result.split()[-1])  # asyncpg returns e.g. "DELETE 42"
    except (ValueError, IndexError, AttributeError):
        return 0
