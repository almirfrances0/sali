"""Learn from failure (spec §18): a verified tool failure becomes a retrievable episodic memory.

When Sali later faces a similar task, retrieval surfaces "last time this failed with X" so it
doesn't blindly repeat the mistake. Each failure is a functional claim keyed by its execution id,
so consolidating repeatedly never duplicates it. Deterministic — the record is what happened.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer


async def record_failures(conn: Any, *, limit: int = 50) -> int:
    """Record recent verified failures not yet captured. Caller owns the transaction."""
    rows = await conn.fetch(
        "SELECT te.id, te.tool_name, te.error, te.plan, r.user_input "
        "FROM tool_execution te LEFT JOIN agent_runs r ON r.run_id = te.run_id "
        "WHERE te.status = 'verified_failure' AND NOT EXISTS ("
        "  SELECT 1 FROM memory m WHERE m.claim_key = 'failure:' || te.id::text "
        "    AND m.valid_until IS NULL) "
        "ORDER BY te.started_at DESC LIMIT $1",
        limit,
    )
    count = 0
    for row in rows:
        plan = row["plan"] or {}
        command = (plan.get("args") or {}).get("command") if isinstance(plan, dict) else None
        doing = f" running `{command}`" if command else ""
        because = f" — {row['error'][:200]}" if row["error"] else ""
        task = f" (task: {row['user_input'][:120]})" if row["user_input"] else ""
        content = f"A past attempt failed: the {row['tool_name']} tool{doing} failed{because}.{task}"
        await memory_writer.remember(
            conn, layer=MemoryLayer.EPISODIC, content=content,
            source=MemorySource.SYSTEM_OBSERVATION, functional=True,
            claim_key=f"failure:{row['id']}", importance=0.5, obs_conf=1.0,
        )
        count += 1
    return count
