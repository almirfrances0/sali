"""Tool advice — let Sali ask its OWN toolset what to use (spec §21/§53).

Before improvising an unfamiliar command, Sali can ask which installed tools serve a task (ranked by
how reliably they've worked on THIS machine and how safe they are), and what a given tool's
alternatives are. Grounded in the tool inventory + capability graph + learned experience — so the
answer is evidence, not a guess. Read-only.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class _Advice(Tool):
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.READ})
    idempotent = True


class RecommendTool(_Advice):
    name = "recommend_tool"
    description = (
        "Find which installed tools can do a task (e.g. 'capture network packets', 'crack a hash', "
        "'scan ports', 'process json'), ranked by how reliably they've worked on this machine and how "
        "safe they are. Use it before running an unfamiliar command — prefer a proven, safe tool. "
        "Empty means nothing installed provides that capability (you may need to install one)."
    )
    parameters = {
        "type": "object",
        "properties": {"task": {"type": "string", "description": "The task/capability you need."}},
        "required": ["task"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.catalog is None:
            return ToolResult(ok=False, display="no tool catalog", error="tool catalog isn't available")
        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult(ok=False, display="need a task", error="task is required")
        tools = await ctx.catalog.suggest(task)
        if not tools:
            return ToolResult(ok=True, output={"tools": []},
                              display="no installed tool matches that — may need to install one")
        top = ", ".join(f"{t['tool']}" for t in tools[:5])
        return ToolResult(ok=True, output={"tools": tools}, display=f"suggested: {top}")


class ToolAlternatives(_Advice):
    name = "tool_alternatives"
    description = (
        "Given a tool, list other installed tools that do a similar job (share a capability) — use it "
        "when a tool is missing, failing, or unsuitable and you want a substitute. Empty means no known "
        "alternative is installed."
    )
    parameters = {
        "type": "object",
        "properties": {"tool": {"type": "string", "description": "The tool to find alternatives to."}},
        "required": ["tool"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.catalog is None:
            return ToolResult(ok=False, display="no tool catalog", error="tool catalog isn't available")
        tool = str(args.get("tool", "")).strip()
        if not tool:
            return ToolResult(ok=False, display="need a tool", error="tool is required")
        alts = await ctx.catalog.alternatives(tool)
        return ToolResult(ok=True, output={"alternatives": alts},
                          display=(f"alternatives: {', '.join(alts)}" if alts else "no known alternative"))


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (RecommendTool(), ToolAlternatives()):
        registry.register(tool)
