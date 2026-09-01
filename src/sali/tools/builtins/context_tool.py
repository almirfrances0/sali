"""Decision-ledger + phase tools (Prompt 6 §30-32).

`record_decision` captures a key task decision (and why) so it survives compaction — especially a user
correction like "use PostgreSQL, not SQLite", which must never be forgotten. `start_phase`/`complete_phase`
let a large task progress in phases so only the current phase plus prior phase summaries enter context.
All are durable, structured task state — the model manages them, but the state lives in PostgreSQL.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


async def _current_task(ctx: ToolContext) -> Any:
    if ctx.tasks is None:
        return None
    return await ctx.tasks.current()


class RecordDecision(Tool):
    name = "record_decision"
    description = (
        "Record a key decision for the current task — a technology/approach choice, a constraint, or a "
        "user correction (e.g. 'use PostgreSQL, not SQLite', 'work in this directory') — with the reason. "
        "It becomes durable state that survives compaction and restart, so it is never forgotten. If it "
        "replaces an earlier decision, pass supersedes to retire the old one (never keep two contradictory "
        "active decisions). Record user corrections here immediately."
    )
    parameters = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "description": "The decision, in one line."},
            "reason": {"type": "string", "description": "Why — the evidence or requirement behind it."},
            "source": {"type": "string", "enum": ["user", "sali", "skill", "research", "reviewer"],
                       "description": "Where it came from. Use 'user' for a user instruction/correction."},
            "supersedes": {"type": "string", "description": "Optional: a prior decision_id this replaces."},
        },
        "required": ["decision"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.decisions is None:
            return ToolResult(ok=False, display="no ledger", error="the decision ledger isn't available")
        task = await _current_task(ctx)
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to decide for")
        decision = str(args.get("decision", "")).strip()
        if not decision:
            return ToolResult(ok=False, display="need decision", error="a decision is required")
        supersedes = None
        sup = str(args.get("supersedes", "")).strip()
        if sup:
            try:
                supersedes = UUID(sup)
            except ValueError:
                supersedes = None
        did = await ctx.decisions.record(
            task.id, decision=decision, reason=str(args.get("reason", "")).strip() or None,
            source=str(args.get("source", "sali")).strip() or "sali", run_id=ctx.run_id,
            supersedes=supersedes)
        return ToolResult(ok=True, output={"decision_id": str(did), "decision": decision},
                          display=f"recorded decision: {decision[:70]}")


class StartPhase(Tool):
    name = "start_phase"
    description = (
        "Begin a new phase of a large multi-stage task (e.g. 'Backend', 'Frontend', 'Testing'). Any "
        "current phase is completed first; pass a short summary of what that phase accomplished. Only the "
        "current phase plus prior phase summaries normally enter context, keeping it small."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The new phase's name."},
            "prior_summary": {"type": "string",
                              "description": "Optional: a short summary of the phase just completed."},
        },
        "required": ["name"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.phases is None:
            return ToolResult(ok=False, display="no phases", error="phases aren't available")
        task = await _current_task(ctx)
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task")
        name = str(args.get("name", "")).strip()
        if not name:
            return ToolResult(ok=False, display="need name", error="a phase name is required")
        phase = await ctx.phases.start(
            task.id, name, summary_of_prior=str(args.get("prior_summary", "")).strip() or None)
        return ToolResult(ok=True, output=phase, display=f"started phase {phase['seq']}: {name}")


class CompletePhase(Tool):
    name = "complete_phase"
    description = (
        "Mark the current task phase complete with a short structured summary (objective, completed work, "
        "verified artifacts, decisions, remaining risks). The summary is durable and carries into later "
        "phases' context."
    )
    parameters = {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "The phase summary."}},
        "required": ["summary"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.phases is None:
            return ToolResult(ok=False, display="no phases", error="phases aren't available")
        task = await _current_task(ctx)
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task")
        await ctx.phases.complete(task.id, summary=str(args.get("summary", "")).strip() or None)
        return ToolResult(ok=True, output={"completed": True}, display="phase completed")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (RecordDecision(), StartPhase(), CompletePhase()):
        registry.register(tool)
