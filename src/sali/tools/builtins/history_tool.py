"""Authoritative tool-execution history (spec §2).

The runtime already records every tool call in order; the model must NEVER infer "which call came before
which" from conversation text (an architectural smell that caused a long reasoning loop in the audit).
This exposes that record deterministically: each execution with its run/session, its sequence number
WITHIN the run, sanitized arguments + a stable arguments hash, timing, status, a result summary + hash,
and any error. Read-only; the data is the ground truth, not a reconstruction.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext


def _stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def execution_record(row: dict[str, Any]) -> dict[str, Any]:
    """Build one deterministic execution record from a tool_execution row (+ its run's session)."""
    plan = row["plan"] or {}
    args = plan.get("args", {})
    observed = row["observed"] or {}
    if row["success"] is True:
        summary = observed.get("display") or "ok"
    elif row["success"] is False:
        summary = observed.get("display") or "failed"
    else:
        summary = None
    return {
        "execution_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "session_id": str(row["session_id"]) if row["session_id"] else None,
        "sequence_number": int(row["seq"]),   # authoritative order WITHIN the run
        "tool_name": row["tool_name"],
        "arguments_hash": _stable_hash(args),
        "sanitized_arguments": args,           # already redacted at write time
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "completed_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "status": row["status"],
        "result_summary": summary,
        "result_hash": _stable_hash(observed) if observed else None,
        "error": row["error"],
    }


_HISTORY_SQL = (
    "SELECT te.id, te.run_id, ar.session_id, te.tool_name, te.plan, te.observed, te.status, "
    "  te.success, te.error, te.started_at, te.finished_at, "
    "  row_number() OVER (PARTITION BY te.run_id ORDER BY te.started_at, te.id) AS seq "
    "FROM tool_execution te LEFT JOIN agent_runs ar ON ar.run_id = te.run_id "
    "{where} ORDER BY te.started_at DESC, te.id DESC LIMIT $1"
)


class ToolHistory(Tool):
    name = "tool_history"
    description = (
        "Show your recent tool executions in their authoritative, deterministic order — what ran, when, "
        "in what sequence within each run, and how each fared. ALWAYS use this instead of trying to "
        "reconstruct the order of your actions from the conversation: the runtime records the exact "
        "order and result, so you never have to guess."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "number", "description": "How many recent executions (default 15)."},
            "run_id": {"type": "string", "description": "Restrict to one run (optional)."},
        },
    }
    risk_level = RiskLevel.R0
    capabilities = frozenset({Capability.READ})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.pool is None:
            return ToolResult(ok=False, display="no datastore", error="execution history isn't available")
        limit = max(1, min(int(args.get("limit") or 15), 100))
        run_id = str(args.get("run_id") or "").strip()
        params: list[Any] = [limit]
        where = ""
        if run_id:
            params.append(run_id)
            where = "WHERE te.run_id = $2"
        async with ctx.pool.acquire() as conn:
            rows = await conn.fetch(_HISTORY_SQL.format(where=where), *params)
        executions = [execution_record(dict(r)) for r in rows]
        return ToolResult(ok=True, output={"executions": executions},
                          display=f"{len(executions)} recent tool execution(s)")


def register_builtins(registry: Any) -> None:
    registry.register(ToolHistory())
