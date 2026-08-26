"""Task tools — Sali runs persistent, multi-step work that survives restarts (spec §24).

For a job with several steps ("investigate the VPS", "build and deploy the site"), Sali lays it out
with plan_task; the task and its steps are stored durably and shown back in context every turn, so
Sali resumes exactly where it left off even after a restart. advance_task marks progress; the task
finishes on its own when every step is done, or Sali closes it with finish_task.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_STEP_STATES = ["done", "failed", "running", "skipped"]


class PlanTask(Tool):
    name = "plan_task"
    description = (
        "Start a persistent, multi-step task so you can carry it across turns (and restarts) without "
        "losing your place. Give the objective and the ordered steps. Use this for real multi-step "
        "work — an investigation, a build-and-deploy — not for a one-shot answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "What the whole task is for, in one line."},
            "steps": {"type": "array", "items": {"type": "string"},
                      "description": "The ordered steps to do it."},
        },
        "required": ["objective", "steps"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        objective = str(args.get("objective", "")).strip()
        steps = [str(s).strip() for s in (args.get("steps") or []) if str(s).strip()]
        if not objective or not steps:
            return ToolResult(ok=False, display="need objective + steps",
                              error="an objective and at least one step are required")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        await ctx.tasks.create(objective, steps)
        return ToolResult(
            ok=True,
            output={"objective": objective, "steps": steps, "count": len(steps)},
            display=f"planned '{objective}' ({len(steps)} steps)",
        )


class AdvanceTask(Tool):
    name = "advance_task"
    description = (
        "Update your progress on the current task: mark a step done, failed, running, or skipped "
        "(with an optional note of what happened). The task completes on its own once every step is "
        "done. Steps are numbered from 1."
    )
    parameters = {
        "type": "object",
        "properties": {
            "step": {"type": "integer", "description": "The step number (1-based)."},
            "status": {"type": "string", "enum": _STEP_STATES},
            "note": {"type": "string", "description": "Optional: what happened on this step."},
        },
        "required": ["step", "status"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to advance")
        step = int(args.get("step") or 0)
        status = str(args.get("status", "")).strip()
        if status not in _STEP_STATES:
            return ToolResult(ok=False, display="bad status", error=f"status must be one of {_STEP_STATES}")
        note = str(args.get("note", "")).strip() or None
        updated = await ctx.tasks.advance(task.id, step, status, note=note)
        return ToolResult(
            ok=True,
            output={"objective": updated.objective, "task_status": updated.status,
                    "step": step, "step_status": status},
            display=f"step {step} → {status}"
            + (" (task done)" if updated.status == "done" else ""),
        )


class FinishTask(Tool):
    name = "finish_task"
    description = (
        "Close the current task explicitly — 'done' when it's complete, or 'abandoned' if you're "
        "stopping it — with a short result summary. (A task also finishes on its own when all its "
        "steps are done.)"
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "abandoned", "failed"]},
            "result": {"type": "string", "description": "A short summary of the outcome."},
        },
        "required": ["status"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to finish")
        status = str(args.get("status", "done")).strip()
        result = str(args.get("result", "")).strip() or None
        await ctx.tasks.finish(task.id, status=status, result=result)
        return ToolResult(ok=True, output={"objective": task.objective, "status": status},
                          display=f"task {status}")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (PlanTask(), AdvanceTask(), FinishTask()):
        registry.register(tool)
