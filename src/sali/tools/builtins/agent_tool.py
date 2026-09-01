"""Agent-coordination tools (Cognitive OS §16/§44): delegate one bounded sub-objective to a subagent,
and pause the task to ask the user a clarifying question. Both are runtime-mediated — the durable state
and the 0-or-1 subagent invariant live in the runtime, not in the model.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class Delegate(Tool):
    name = "delegate"
    description = (
        "Delegate ONE bounded, self-contained sub-objective to a subagent — research an API, inspect a "
        "directory, analyze an error, compare two approaches — and get concise findings back to use. "
        "Only one subagent runs at a time and you stay in charge; the subagent cannot change the task or "
        "the project. Use it for a focused investigation you'd rather hand off than interleave."
    )
    parameters = {
        "type": "object",
        "properties": {"objective": {"type": "string",
                                     "description": "The single bounded objective for the subagent."}},
        "required": ["objective"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset()
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # SUBAGENT DISABLED (Prompt 12 §7 + user directive): Sali is one executive agent. A subagent is a
        # second reasoning stream that could issue a concurrent model call, and this machine cannot load
        # `sali:latest` twice. Delegation always refuses — do the work in the one foreground stream.
        return ToolResult(
            ok=False, display="single agent — no delegation",
            error="delegation is disabled: Sali is a single executive agent (no subagents). Do the work "
                  "yourself in the current reasoning stream.")


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
    # The `delegate` tool is intentionally NOT registered — Sali is a single executive agent with no
    # subagents (Prompt 12 §7 + user directive: never a second reasoning stream, never sali:latest loaded
    # twice). The class is kept (and still refuses if invoked) only for backward compatibility. Only the
    # user-clarification tool is advertised.
    registry.register(AskUser())
