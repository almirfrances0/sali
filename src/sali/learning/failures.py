"""Learn from failure (spec §18): a verified tool failure becomes a retrievable episodic memory.

When Sali later faces a similar task, retrieval surfaces "last time this failed with X" so it
doesn't blindly repeat the mistake. Each failure is a functional claim keyed by its execution id,
so consolidating repeatedly never duplicates it. Deterministic — the record is what happened.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer


def _command_of(plan: Any) -> str | None:
    return (plan.get("args") or {}).get("command") if isinstance(plan, dict) else None


async def record_failures(conn: Any, *, limit: int = 50) -> int:
    """Record recent verified failures not yet captured — and, when a later step in the same run
    fixed it, the correction too (§18's diagnosis→correction). Caller owns the transaction."""
    rows = await conn.fetch(
        "SELECT te.id, te.run_id, te.tool_name, te.error, te.plan, te.started_at, r.user_input "
        "FROM tool_execution te LEFT JOIN agent_runs r ON r.run_id = te.run_id "
        "WHERE te.status = 'verified_failure' AND NOT EXISTS ("
        "  SELECT 1 FROM memory m WHERE m.claim_key = 'failure:' || te.id::text "
        "    AND m.valid_until IS NULL) "
        "ORDER BY te.started_at DESC LIMIT $1",
        limit,
    )
    count = 0
    for row in rows:
        command = _command_of(row["plan"])
        # The correction: the next successful call of the same tool later in the same run.
        fix = await conn.fetchrow(
            "SELECT plan FROM tool_execution WHERE run_id=$1 AND tool_name=$2 "
            "  AND status='verified_success' AND started_at > $3 ORDER BY started_at LIMIT 1",
            row["run_id"], row["tool_name"], row["started_at"],
        )
        fix_command = _command_of(fix["plan"]) if fix else None

        doing = f" running `{command}`" if command else ""
        because = f" — {row['error'][:200]}" if row["error"] else ""
        task = f" (task: {row['user_input'][:120]})" if row["user_input"] else ""
        content = f"A past attempt failed: the {row['tool_name']} tool{doing} failed{because}.{task}"
        learned_fix = bool(fix_command and fix_command != command)
        if learned_fix:
            content += f" What fixed it: `{fix_command}`."
        await memory_writer.remember(
            conn, layer=MemoryLayer.EPISODIC, content=content,
            source=MemorySource.SYSTEM_OBSERVATION, functional=True,
            claim_key=f"failure:{row['id']}", importance=0.6 if learned_fix else 0.5, obs_conf=1.0,
        )
        count += 1
    return count
