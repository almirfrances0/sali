"""Learn from failure (spec §18): a verified tool failure becomes a retrievable episodic memory.

When Sali later faces a similar task, retrieval surfaces "last time this failed with X" so it
doesn't blindly repeat the mistake. Each failure is a functional claim keyed by the LESSON — the tool
plus the program or operation that failed — so the same mistake made ten times is one memory that
`_resolve_claim` supersedes in place. Deterministic — the record is what happened.

It used to be keyed by the tool_execution id. That prevented re-processing one execution twice, but it
is not what "same lesson, one memory" means: every fresh occurrence got a fresh row, the identical
`cd /home/almir/...` failure was stored 55 times, and by 2026-09-03 tool-failure episodes were 46% of
live memory (101 of 219 rows, only 15 distinct). Retrieval then put "the execute_command tool failed"
at the top of Sali's context on every turn, and Sali stopped reaching for tools at all.
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


def _step_summary(tool_name: str, plan: Any) -> str:
    """A compact summary of what a tool execution did, for both command and non-command tools."""
    command = _command_of(plan)
    if command:
        return command
    # For non-command tools, summarize the key arg
    args = (plan.get("args") or {}) if isinstance(plan, dict) else {}
    for key in ("path", "query", "url", "entity", "name", "to", "subject"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return f"{tool_name}({val[:60]})"
    return tool_name


def _lesson_key(tool_name: str, step: str, command: str | None) -> str:
    """The identity of the LESSON, not of the execution.

    Two failures are the same lesson when the same tool failed doing the same kind of thing. For a
    command tool that is the program being run (`_binary`), so fifty-five failures of `cd ...` are one
    claim rather than fifty-five memories. For other tools the step summary already carries the operation
    and is bounded to 60 characters.
    """
    signature = (_binary(command) if command else step) or tool_name
    return f"failure:{tool_name}:{signature[:60]}"


async def record_failures(conn: Any, *, limit: int = 50) -> int:
    """Record recent verified failures not yet captured — and, when a later step in the same run
    fixed it, the correction too (§18's diagnosis→correction). Caller owns the transaction."""
    rows = await conn.fetch(
        "SELECT te.id, te.run_id, te.tool_name, te.error, te.plan, te.started_at, r.user_input "
        "FROM tool_execution te LEFT JOIN agent_runs r ON r.run_id = te.run_id "
        # Only RECENT failures — old dev-time failures are noise, not lessons. Bounded so the whole
        # historical backlog isn't turned into hundreds of episodic memories.
        "WHERE te.status = 'verified_failure' AND te.started_at > now() - interval '2 days' "
        # Has this EXECUTION already been turned into a lesson? Deliberately not filtered by
        # `valid_until IS NULL`: a retired or superseded memory still means the execution was
        # processed. With the old filter, retiring duplicate failure memories caused the next
        # consolidation to recreate every one of them. Both key shapes are checked so executions
        # recorded under the old per-execution claim_key are not re-recorded under the new one.
        "  AND NOT EXISTS (SELECT 1 FROM memory m "
        "    WHERE m.claim_key = 'failure:' || te.id::text "
        "       OR m.structured->>'tool_execution_id' = te.id::text) "
        "ORDER BY te.started_at DESC LIMIT $1",
        limit,
    )
    count = 0
    for row in rows:
        command = _command_of(row["plan"])
        step = _step_summary(row["tool_name"], row["plan"])
        # The correction: the next successful call of the same tool later in the same run.
        fix = await conn.fetchrow(
            "SELECT plan FROM tool_execution WHERE run_id=$1 AND tool_name=$2 "
            "  AND status='verified_success' AND started_at > $3 ORDER BY started_at LIMIT 1",
            row["run_id"], row["tool_name"], row["started_at"],
        )
        fix_command = _command_of(fix["plan"]) if fix else None
        fix_step = _step_summary(row["tool_name"], fix["plan"]) if fix else None

        doing = f" running `{step}`" if step != row["tool_name"] else ""
        because = f" — {row['error'][:200]}" if row["error"] else ""
        # A genuine correction: for command tools, a later success that RE-RUNS the same program
        # with a change. For non-command tools, any later success of the same tool counts.
        if command and fix_command:
            learned_fix = bool(fix_command != command and _binary(command) == _binary(fix_command))
        else:
            # Non-command tool: a later success of the same tool is a correction
            learned_fix = bool(fix and fix_step and fix_step != step)
        # Did this failure stop REAL WORK? A failure with no fix is normally noise — but one that
        # blocked a task step is a lesson-in-waiting: it is what the learning queue researches and
        # what `learned → retry` closes. Without this, a blocked step left nothing behind to close.
        blocked = await conn.fetchrow(
            "SELECT task_id, step_seq FROM task_execution WHERE execution_id = $1 LIMIT 1", row["id"])
        if not learned_fix and blocked is None:
            continue  # §18 is failure→CORRECTION — a bare one-off error is noise, not a lesson
        if learned_fix:
            content = (f"A past attempt failed: the {row['tool_name']} tool{doing} failed{because}. "
                       f"What fixed it: `{fix_step or fix_command}`.")
        else:
            content = (f"An attempt failed and is still unresolved: the {row['tool_name']} "
                       f"tool{doing} failed{because}. No fix is known yet.")
        note = f"task: {row['user_input'][:120]}" if row["user_input"] else None
        # A STRUCTURED incident (§24/§25) alongside the text, so recall gives Sali the whole shape, not
        # a sentence. Everything here is FACT from the tool_execution rows; the *cause* is deliberately
        # only a HYPOTHESIS (§25: a failure never proves its cause — the correction is what's verified).
        incident = {
            "kind": "incident",
            "objective": (row["user_input"] or "")[:200] or None,
            "tool": row["tool_name"],
            "failed_command": step,
            "error": (row["error"] or "")[:400] or None,
            # An OPEN incident asserts no cause and no fix. Inventing either is exactly the
            # confabulation §25 forbids; it carries the blocked step instead, so the lesson that
            # eventually closes it can find its way back to the work.
            "hypothesis": (f"the fix `{fix_step or fix_command}` suggests the failure was addressable"
                           if learned_fix else None),
            "correction": (fix_step or fix_command) if learned_fix else None,
            "verified": bool(learned_fix),  # a verified_success later in the same run
            "outcome": "resolved" if learned_fix else "open",
            "task_id": str(blocked["task_id"]) if blocked else None,
            "step_seq": int(blocked["step_seq"]) if blocked and blocked["step_seq"] is not None else None,
            # Which execution produced this lesson. The claim_key now names the lesson, so this is what
            # tells the consolidator that this particular execution has already been accounted for.
            "tool_execution_id": str(row["id"]),
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
            claim_key=_lesson_key(row["tool_name"], step, command),
            # An open incident is worth less than a solved one — it is a question, not an answer.
            # Same claim_key either way, so the resolved lesson SUPERSEDES the open one when it lands.
            importance=0.6 if learned_fix else 0.45, obs_conf=1.0, note=note,
            structured=incident,
        )
        count += 1
    return count
