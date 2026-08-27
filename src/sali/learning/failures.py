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


def _binary(command: str | None) -> str:
    """The first token of a command — the program being run. Two commands are 'the same fix target'
    only if they share this, so 'docker … up' → 'docker … --env-file up' counts but a random later
    success of another command does not."""
    parts = (command or "").strip().split(maxsplit=1)
    return parts[0] if parts else ""


async def record_failures(conn: Any, *, limit: int = 50) -> int:
    """Record recent verified failures not yet captured — and, when a later step in the same run
    fixed it, the correction too (§18's diagnosis→correction). Caller owns the transaction."""
    rows = await conn.fetch(
        "SELECT te.id, te.run_id, te.tool_name, te.error, te.plan, te.started_at, r.user_input "
        "FROM tool_execution te LEFT JOIN agent_runs r ON r.run_id = te.run_id "
        # Only RECENT failures — old dev-time failures are noise, not lessons. Bounded so the whole
        # historical backlog isn't turned into hundreds of episodic memories.
        "WHERE te.status = 'verified_failure' AND te.started_at > now() - interval '2 days' "
        "  AND NOT EXISTS (SELECT 1 FROM memory m WHERE m.claim_key = 'failure:' || te.id::text "
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
        # Content is the failure SIGNATURE (tool + command + error) — NOT the per-turn task, which
        # varies and would record the same recurring failure dozens of times (a bloat of near-dupes,
        # and the (layer, content_hash) collision Almir kept seeing). The task goes in the note.
        # A genuine correction: a later success that RE-RUNS the same program with a change — not any
        # unrelated later command that happened to succeed (which produced spurious failure→fix noise).
        learned_fix = bool(command and fix_command and fix_command != command
                           and _binary(command) == _binary(fix_command))
        if not learned_fix:
            continue  # §18 is failure→CORRECTION — a bare one-off error is noise, not a lesson
        content = (f"A past attempt failed: the {row['tool_name']} tool{doing} failed{because}. "
                   f"What fixed it: `{fix_command}`.")
        note = f"task: {row['user_input'][:120]}" if row["user_input"] else None
        # A STRUCTURED incident (§24/§25) alongside the text, so recall gives Sali the whole shape, not
        # a sentence. Everything here is FACT from the tool_execution rows; the *cause* is deliberately
        # only a HYPOTHESIS (§25: a failure never proves its cause — the correction is what's verified).
        incident = {
            "kind": "incident",
            "objective": (row["user_input"] or "")[:200] or None,
            "tool": row["tool_name"],
            "failed_command": command,
            "error": (row["error"] or "")[:400] or None,
            "hypothesis": f"the fix `{fix_command}` suggests the failure was addressable by re-running "
                          f"{_binary(command)} differently",
            "correction": fix_command,
            "verified": True,  # the correction was a verified_success later in the same run
            "outcome": "resolved",
        }
        # Record each distinct failure→fix once (dedup by content signature) — same lesson, one memory.
        if await conn.fetchval(
            "SELECT 1 FROM memory WHERE layer = 'episodic'::memory_layer "
            "AND content_hash = digest($1, 'sha256') AND valid_until IS NULL LIMIT 1", content,
        ):
            continue
        await memory_writer.remember(
            conn, layer=MemoryLayer.EPISODIC, content=content,
            source=MemorySource.SYSTEM_OBSERVATION, functional=True,
            claim_key=f"failure:{row['id']}", importance=0.6, obs_conf=1.0, note=note,
            structured=incident,
        )
        count += 1
    return count
