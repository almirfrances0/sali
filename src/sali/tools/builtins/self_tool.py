"""Self-introspection — let Sali report its OWN state and architecture (spec §6/§7/§41/§71).

When Almir asks "what are you working on?", "what are you unsure about?", or "how do you work?", Sali
answers from its real runtime self-model — identity, self-knowledge, current focus/task, how it last
fared, and its open uncertainties — instead of inventing. Read-only.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class SelfState(Tool):
    name = "self_state"
    description = (
        "Report your own current state and architecture — use it when Almir asks what you're doing, "
        "what you're working on, what you're unsure about, or how you work. Returns your identity, a "
        "truthful description of how you're built, your current focus and task, how you last fared, and "
        "your open uncertainties. Answer from THIS, never invent details about yourself."
    )
    parameters = {"type": "object", "properties": {}}
    risk_level = RiskLevel.R0
    capabilities = frozenset({Capability.READ})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.self_model is None:
            return ToolResult(ok=False, display="no self-model", error="self-state isn't available")
        report = await ctx.self_model.report()
        focus = report.get("current_focus") or report.get("current_task") or "nothing in particular"
        return ToolResult(ok=True, output=report, display=f"self-state (focus: {focus})")


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(SelfState())
