"""Just-in-time research tools (Prompt 5 §14-20).

`research_task` searches the web for something Sali genuinely needs to continue the CURRENT task and
saves the finding as durable, task-linked evidence. `record_lesson` proposes a reusable lesson from a
finding that actually helped — it becomes durable memory only once the task's completion is verified by
the reviewer (a failed experiment never becomes permanent knowledge). Neither is a substitute for real
execution/artifact evidence; the reviewer still decides completion.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class ResearchTask(Tool):
    name = "research_task"
    description = (
        "Search the web for something you genuinely need to continue the CURRENT task — current docs, "
        "an unfamiliar API or error, a changed install/config, a version incompatibility. Returns a "
        "bounded summary + sources and saves it as durable evidence for THIS task. Use it only when the "
        "local skills/knowledge don't cover it — not for every ordinary decision. What you read is "
        "guidance, not proof: verify by actually running things."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The specific question to look up."},
            "step": {"type": "integer", "description": "Optional: the task step this is for."},
        },
        "required": ["query"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.research is None:
            return ToolResult(ok=False, display="no research", error="research isn't available here")
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, display="need query", error="a query is required")
        step = args.get("step")
        out = await ctx.research.research(query, step_seq=int(step) if step is not None else None)
        if not out.get("ok"):
            # research failing must NOT lose task progress — return a soft result, keep working (§21)
            return ToolResult(
                ok=True, output={"researched": False, "reason": out.get("reason"), "query": query},
                display=f"research: nothing found for '{query[:60]}' — continuing")
        return ToolResult(
            ok=True,
            output={"researched": True, "research_id": out["research_id"], "summary": out["summary"],
                    "source": out.get("source"), "sources": out.get("sources")},
            display=f"researched '{query[:60]}' — {len(out.get('sources') or [])} source(s)")


class RecordLesson(Tool):
    name = "record_lesson"
    description = (
        "After web research materially helped AND you verified it worked (you ran it and saw the real "
        "evidence), record a short reusable lesson. It is saved as a CANDIDATE and becomes durable "
        "memory only once this task's completion is verified by the reviewer — a failed experiment "
        "never becomes permanent knowledge."
    )
    parameters = {
        "type": "object",
        "properties": {
            "lesson": {"type": "string", "description": "The reusable lesson, in one or two sentences."},
            "source": {"type": "string", "description": "Optional: the evidence/source URL."},
            "research_id": {"type": "string", "description": "Optional: the research_id it came from."},
        },
        "required": ["lesson"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.research is None:
            return ToolResult(ok=False, display="no research", error="research isn't available here")
        lesson = str(args.get("lesson", "")).strip()
        if not lesson:
            return ToolResult(ok=False, display="need lesson", error="a lesson is required")
        out = await ctx.research.record_lesson(
            lesson, source=str(args.get("source", "")).strip() or None,
            research_id=str(args.get("research_id", "")).strip() or None)
        return ToolResult(
            ok=True, output=out,
            display="recorded a candidate lesson (promotes to memory only after the task is verified)")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (ResearchTask(), RecordLesson()):
        registry.register(tool)
