"""PendingAction continuity (spec §2/§6/§10).

An executable command Sali PROPOSES but does not run is a real state: proposed → (delegated) → running →
succeeded/failed. Rather than invent a table, this reuses the `tool_execution` lifecycle whose `'planned'`
status has existed since 0001 but was never written — closing the §6 honesty gap (a proposal now leaves a
durable row) and giving "run it" something deterministic to resolve against. When Almir delegates, the loop
reuses this row's id so it transitions planned → executing → verified_* on the SAME row, through the normal
policy/execute/verify pipeline — the command is never reconstructed by the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sali.core.ids import new_id


@dataclass(slots=True)
class PendingAction:
    exec_id: UUID
    tool_name: str
    arguments: dict[str, Any]
    description: str
    command: str | None  # the shell command, when the tool is a command tool (for surfacing)


async def _session_of(conn: Any, run_id: UUID) -> UUID | None:
    return cast("UUID | None", await conn.fetchval("SELECT session_id FROM agent_runs WHERE run_id = $1", run_id))


async def supersede(conn: Any, session_id: UUID) -> None:
    """A newer proposal (or a resolved one) obsoletes any earlier still-'planned' rows for this session,
    so only the most recent proposal is ever pending (§10 priority: most recent executable proposal)."""
    await conn.execute(
        "UPDATE tool_execution SET status='aborted' "
        "WHERE status='planned' AND run_id IN (SELECT run_id FROM agent_runs WHERE session_id=$1)",
        session_id,
    )


async def capture(
    conn: Any, run_id: UUID, tool_name: str, args: dict[str, Any], *, description: str = "",
    risk_level: int = 0,
) -> UUID:
    """Record a proposed-but-unrun command as a durable 'planned' tool_execution row. Args are stored
    verbatim (a proposed command is text Sali already showed Almir; the sudo password is never in it — it
    lives in the broker), so the exact command can be re-run faithfully when delegated. Supersedes older
    proposals for the session so 'run it' always resolves the latest."""
    session_id = await _session_of(conn, run_id)
    if session_id is not None:
        await supersede(conn, session_id)
    exec_id = new_id()
    await conn.execute(
        "INSERT INTO tool_execution (id, run_id, run_kind, tool_name, status, danger_level, plan) "
        "VALUES ($1, $2, 'agent_run', $3, 'planned', $4, $5)",
        exec_id, run_id, tool_name, int(risk_level),
        {"args": args, "description": description, "pending": True},
    )
    return exec_id


async def latest_pending(conn: Any, session_id: UUID, *, max_age_min: int = 120) -> PendingAction | None:
    """The freshest still-proposed action for this session (within a freshness bound), or None. This is
    what a delegation phrase ("run it") resolves against — compaction-independent, since it's a durable row."""
    row = await conn.fetchrow(
        "SELECT te.id, te.tool_name, te.plan FROM tool_execution te "
        "JOIN agent_runs r ON te.run_id = r.run_id "
        "WHERE r.session_id = $1 AND te.status = 'planned' "
        "  AND te.started_at > now() - ($2 || ' minutes')::interval "
        "ORDER BY te.started_at DESC LIMIT 1",
        session_id, str(max_age_min),
    )
    if row is None:
        return None
    plan = row["plan"] or {}
    args = plan.get("args") or {}
    command = args.get("command") if isinstance(args, dict) else None
    return PendingAction(
        exec_id=row["id"], tool_name=row["tool_name"], arguments=args,
        description=str(plan.get("description") or ""),
        command=command if isinstance(command, str) else None,
    )
