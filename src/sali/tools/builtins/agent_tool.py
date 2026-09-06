"""Agent-coordination tools (Cognitive OS §44): pause the task to ask the user a clarifying question.

Sali is a single executive agent (Prompt 12 §7 + user directive). Delegation is not a supported
capability — a second reasoning stream could issue a concurrent model call and load `sali:latest`
twice, which this machine cannot handle. Brain-audit turn 8 removed the inert `Delegate` tool
class and its refuse-scaffold (never registered, always refused if invoked, kept "for backward
compat" against no live caller). AskUser is the only agent-coordination tool now.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class AskUser(Tool):
    name = "ask_user"
    description = (
        "Pause the current task and ask the user a clarifying question when you genuinely need their "
        "decision to continue (e.g. two valid deployment targets). The task is saved as "
        "'waiting_for_user' and resumes on their reply — objective, plan, and evidence are all kept. Use "
        "this instead of guessing or inventing an answer."
    )
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "The question for the user."}},
        "required": ["question"],
    }
    risk_level = RiskLevel.R0
    capabilities = frozenset()
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not allowed",
                              error="a subagent cannot ask the user — report back to the primary instead")
        if ctx.clarify is None:
            return ToolResult(ok=False, display="no clarify", error="clarification isn't available here")
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult(ok=False, display="need question", error="a question is required")
        out = await ctx.clarify.ask(question)
        if not out.get("ok"):
            return ToolResult(ok=False, display="cannot ask", error=str(out.get("reason")))
        return ToolResult(ok=True, output=out,
                          display=f"asked the user — task is waiting: {question[:60]}")


def register_builtins(registry: ToolRegistry) -> None:
    # Delegation removed (brain-audit turn 8). Only the user-clarification tool is advertised.
    registry.register(AskUser())
