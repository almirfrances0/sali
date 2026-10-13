"""Schedule tools — Sali sets up its own recurring work (spec §44).

When Almir asks for something on a rhythm ("every morning check the disk and tell me", "remind me
Fridays"), Sali records a durable schedule with schedule_task; the scheduler daemon fires the stored
prompt as a full Sali turn at that time, surviving restarts. cancel_schedule stops one.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class ScheduleTask(Tool):
    name = "schedule_task"
    description = (
        "Set up recurring work: at the given time, Sali will run the prompt as a full turn (with all "
        "its tools). 'when' is an interval like '30m'/'2h'/'1d', or a 5-field cron like '0 9 * * *' "
        "(9am daily). Give it a short unique name so you can cancel it later."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A short unique name for the schedule."},
            "when": {"type": "string", "description": "Interval ('30m','2h','1d') or cron ('0 9 * * *')."},
            "prompt": {"type": "string", "description": "What to do each time it fires, in your own words."},
        },
        "required": ["name", "when", "prompt"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        when = str(args.get("when", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        if not (name and when and prompt):
            return ToolResult(ok=False, display="need name, when, prompt",
                              error="name, when and prompt are all required")
        if ctx.schedules is None:
            return ToolResult(ok=False, display="no scheduler", error="the scheduler isn't available")
        try:
            sched = await ctx.schedules.create(name, when, prompt)
        except ValueError as exc:  # a ScheduleError (bad interval/cron) — a spec that'd never fire
            return ToolResult(ok=False, display="bad schedule", error=str(exc))
        return ToolResult(
            ok=True,
            output={"name": name, "when": when, "next_run": sched.next_run_at.isoformat()},
            display=f"scheduled '{name}' — next {sched.next_run_at:%Y-%m-%d %H:%M}",
        )


class CancelSchedule(Tool):
    name = "cancel_schedule"
    description = "Stop a recurring schedule you set up earlier, by its name."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The schedule's name."}},
        "required": ["name"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        if not name:
            return ToolResult(ok=False, display="need name", error="a schedule name is required")
        if ctx.schedules is None:
            return ToolResult(ok=False, display="no scheduler", error="the scheduler isn't available")
        removed = await ctx.schedules.delete(name)
        if not removed:
            return ToolResult(ok=False, display="not found", error=f"no schedule named '{name}'")
        return ToolResult(ok=True, output={"name": name}, display=f"cancelled '{name}'")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (ScheduleTask(), CancelSchedule()):
        registry.register(tool)
