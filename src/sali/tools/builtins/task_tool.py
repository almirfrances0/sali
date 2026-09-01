"""Task tools — Sali runs persistent, multi-step work that survives restarts (spec §24).

For a job with several steps ("investigate the VPS", "build and deploy the site"), Sali lays it out
with plan_task; the task and its steps are stored durably and shown back in context every turn, so
Sali resumes exactly where it left off even after a restart. advance_task marks progress; the task
finishes on its own when every step is done, or Sali closes it with finish_task.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_STEP_STATES = ["done", "failed", "running", "skipped"]



async def _folder(ctx: ToolContext, method: str, *args: Any, **kwargs: Any) -> Any:
    """Best-effort task-folder mirror op via the task store, so tools never import the tasks layer.
    No-ops silently when the sink does not provide it (e.g. a test fake)."""
    import contextlib
    fn = getattr(getattr(ctx, "tasks", None), method, None)
    if fn is None:
        return None
    with contextlib.suppress(Exception):
        return await fn(*args, **kwargs)
    return None

class PlanTask(Tool):
    name = "plan_task"
    description = (
        "Start a persistent, multi-step task so you can carry it across turns (and restarts) without "
        "losing your place. Give the objective and the ordered steps. Use this for real multi-step "
        "work — an investigation, a build-and-deploy — not for a one-shot answer. "
        "For filesystem/project work, pass workspace_root to declare the project folder — "
        "all file operations will be confined to that folder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "What the whole task is for, in one line."},
            "steps": {"type": "array", "items": {"type": "string"},
                      "description": "The ordered steps to do it."},
            "workspace_root": {"type": "string",
                               "description": "Optional: the project folder for this task. "
                               "All file writes will be confined to this folder."},
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
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")

        # Create the task, then bind its AUTHORITATIVE workspace (Prompt 5): an explicit workspace the
        # user gave → a project folder referenced in the objective → otherwise a deterministic
        # workspace under sali-works/tasks/<task_id>. Never /home directly. This is durable task state,
        # resolved ONCE here and never re-resolved on continuation/recovery/rework.
        explicit_ws = str(args.get("workspace_root", "")).strip() or None
        task = await ctx.tasks.create(objective, steps)
        ws_root = explicit_ws
        with contextlib.suppress(Exception):
            bound = await ctx.tasks.bind_workspace(
                task.id, objective=objective, explicit=explicit_ws,
                sali_works_root=str(ctx.settings.permissions.workspace),
                cwd=str(ctx.settings.permissions.exec_cwd))
            ws_root = bound.get("workspace_root")
        # Activate through the authority system — this handles superseding the old task.
        if ctx.task_authority is not None:
            await ctx.task_authority.activate_task(task.id)
        elif hasattr(ctx.tasks, 'activate'):
            await ctx.tasks.activate(task.id)
        # Log task creation to sali-works (filesystem backup).
        with contextlib.suppress(Exception):
            await _folder(ctx, "save_meta", task.id, objective, steps, workspace_root=ws_root, status="open")
            await _folder(ctx, "log_event", task.id, "task_created",
                         {"objective": objective, "steps": len(steps),
                          "workspace_root": ws_root})
        return ToolResult(
            ok=True,
            output={"objective": objective, "steps": steps, "count": len(steps),
                    "workspace_root": ws_root},
            display=f"planned '{objective}' ({len(steps)} steps)"
            + (f" in {ws_root}" if ws_root else ""),
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
            "checkpoint": {"type": "object", "description": "Optional: save in-step progress (any keys) "
                           "so you resume MID-step, not from scratch, after a restart."},
        },
        "required": ["step", "status"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
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
        # §5: persist in-step progress so a long step resumes mid-way after a restart, not from scratch.
        checkpoint = args.get("checkpoint")
        if isinstance(checkpoint, dict) and checkpoint:
            with contextlib.suppress(Exception):
                await ctx.tasks.checkpoint(task.id, step, checkpoint)
        # §8: when a step is DONE, link the tool_execution that VERIFIED an effect this run — so the
        # step is provably "verified", not merely "reported done". Best-effort; never blocks the mark.
        verified_by: UUID | None = None
        if status == "done" and ctx.pool is not None and ctx.run_id is not None:
            with contextlib.suppress(Exception):
                async with ctx.pool.acquire() as conn:
                    verified_by = await conn.fetchval(
                        "SELECT id FROM tool_execution WHERE run_id=$1 AND status='verified_success' "
                        "ORDER BY finished_at DESC LIMIT 1",
                        ctx.run_id,
                    )
        updated, err = await ctx.tasks.advance(
            task.id, step, status, note=note, verified_by=verified_by, run_id=ctx.run_id)
        if err is not None:
            # Validation failed — return clear error to the LLM
            return ToolResult(ok=False, display=f"step {step} rejected", error=err)
        if updated is None:
            # Task auto-completed and was archived to sali-works/tasks/
            return ToolResult(
                ok=True,
                output={"objective": task.objective, "task_status": "done",
                        "step": step, "step_status": status},
                display=f"step {step} → {status} (task done — archived)",
            )
        # Log step advancement to sali-works (filesystem backup).
        with contextlib.suppress(Exception):
            await _folder(ctx, "log_event", task.id, "step_advanced",
                         {"step": step, "status": status, "note": note,
                          "task_status": updated.status})
        # All steps are complete but the task did NOT auto-archive → the reviewer gate is in effect
        # (Prompt 4): the task is verified by review, never by marking the last step done. Tell the
        # model to run the review rather than assume completion. Surfaces the latest review's findings.
        if updated.next_step is None and updated.status == "running":
            review = None
            with contextlib.suppress(Exception):
                if ctx.reviewer is not None:
                    latest = await ctx.reviewer.latest_review(task.id)
                    review = latest.to_public() if latest is not None else None
            return ToolResult(
                ok=True,
                output={"objective": updated.objective, "task_status": "running",
                        "step": step, "step_status": status, "all_steps_done": True,
                        "review_required": True, "review": review},
                display=f"step {step} → {status} (all steps done — run finish_task to review & complete)",
            )
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
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to finish")
        status = str(args.get("status", "done")).strip()
        result = str(args.get("result", "")).strip() or None

        # The reviewer gate (Prompt 4) lives in the store (the bypass-proof choke point): finish('done')
        # runs a FRESH deterministic review and completes only on PASS. Here we surface a rejection with
        # the concrete findings — read back from the durable review the store just wrote — so the model
        # knows exactly what to fix (same task, no new task). NEEDS_REWORK / BLOCKED keep the task open.
        err = await ctx.tasks.finish(task.id, status=status, result=result, run_id=ctx.run_id)
        if err is not None:
            if err == "review_required" and ctx.reviewer is not None:
                latest = await ctx.reviewer.latest_review(task.id)
                if latest is not None:
                    pub = latest.to_public()
                    verb = "blocked" if latest.status.value == "blocked" else "needs rework"
                    return ToolResult(
                        ok=False,
                        output={"status": "rejected", "reason": "review_required",
                                "task_id": str(task.id), "review_id": str(latest.review_id),
                                "review_status": latest.status.value, "summary": latest.summary,
                                "failed": pub["failures"], "rework": pub["recommendations"]},
                        display=f"review: {verb} — {len(pub['failures'])} issue(s) to fix",
                        error=f"completion rejected — {verb}: {latest.summary}",
                    )
            return ToolResult(ok=False, display="cannot finish task", error=err)
        # The reviewer passed and the task is done — promote any VERIFIED learning candidates into
        # durable memory (Prompt 5 §19). A failed/abandoned task promotes nothing.
        if status == "done" and ctx.research is not None:
            with contextlib.suppress(Exception):
                await ctx.research.promote_verified()
        # Log task completion to sali-works (filesystem backup).
        with contextlib.suppress(Exception):
            await _folder(ctx, "log_event", task.id, "task_finished",
                         {"status": status, "result": result})
        return ToolResult(ok=True, output={"objective": task.objective, "status": status},
                          display=f"task {status}"
                          + (" (review passed)" if status == "done" and ctx.reviewer is not None else ""))


class ReviewTask(Tool):
    name = "review_task"
    description = (
        "Verify — with real evidence — whether the current task is actually complete, the way a careful "
        "engineer checks their own work before declaring it done. This inspects durable state (which "
        "steps are verified, tool executions, artifacts on disk) and returns PASS / NEEDS_REWORK / "
        "BLOCKED with concrete findings. Use it to check before finishing; finish_task also runs it "
        "automatically. It never marks the task done — it only reports what the evidence shows."
    )
    parameters = {"type": "object", "properties": {}}
    risk_level = RiskLevel.R0
    capabilities = frozenset()
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to review")
        if ctx.reviewer is None:
            return ToolResult(ok=False, display="no reviewer",
                              error="the reviewer isn't available in this context")
        review = await ctx.reviewer.review(task.id, run_id=ctx.run_id)
        pub = review.to_public()
        return ToolResult(
            ok=True,
            output={"task_id": str(task.id), "review_id": str(review.review_id),
                    "status": review.status.value, "attempt": review.attempt,
                    "summary": review.summary, "passed": review.passed, "failed": review.failed,
                    "unknown": review.unknown, "failures": pub["failures"],
                    "rework": pub["recommendations"]},
            display=f"review: {review.status.value} — {review.summary}",
        )


class ConfirmTask(Tool):
    name = "confirm_task"
    description = (
        "You're satisfied with the task result — confirm it and clean up the task folder from "
        "sali-works/tasks/. Call this ONLY when Almir says he's happy with the result, or when "
        "you've verified the output is correct. The memory of this task persists in your learning "
        "pipeline even after the folder is deleted."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task UUID to confirm. "
                        "Use the current active task's ID."},
            "satisfaction": {"type": "string",
                             "description": "Why you're confirming — e.g. 'Almir confirmed', "
                             "'verified output matches requirements'."},
        },
        "required": ["task_id"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from uuid import UUID

        task_id_str = str(args.get("task_id", "")).strip()
        if not task_id_str:
            return ToolResult(ok=False, display="need task_id", error="task_id is required")
        try:
            task_id = UUID(task_id_str)
        except ValueError:
            return ToolResult(ok=False, display="bad task_id", error="invalid UUID")
        satisfaction = str(args.get("satisfaction", "")).strip() or "confirmed"
        # Log the confirmation
        with contextlib.suppress(Exception):
            await _folder(ctx, "log_event", task_id, "task_confirmed", {"satisfaction": satisfaction})
        # Delete the task folder from sali-works/tasks/
        deleted = await _folder(ctx, "delete_folder", task_id)
        return ToolResult(
            ok=True,
            output={"task_id": task_id_str, "folder_deleted": deleted,
                    "satisfaction": satisfaction},
            display=f"task confirmed — folder {'deleted' if deleted else 'already gone'}",
        )


class ModifyTask(Tool):
    name = "modify_task"
    description = (
        "Modify the current task's steps when Almir asks for changes. Use this when Almir says "
        "'change X', 'add a step', 'remove that', 'redo step 3', or 'modify the plan'. "
        "You can add, remove, or replace steps. The task keeps its existing progress — "
        "only the specified steps change. This is for mid-task modifications, not new tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "replace", "reset"],
                       "description": "What to do: add a new step, remove a step, "
                       "replace a step's description, or reset a step to pending."},
            "step": {"type": "integer",
                     "description": "The step number (1-based). Required for remove/replace/reset."},
            "description": {"type": "string",
                            "description": "New step description. Required for add/replace."},
            "after_step": {"type": "integer",
                           "description": "For 'add': insert after this step number. "
                           "Omit to append at the end."},
        },
        "required": ["action"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database pool not available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task",
                              error="there's no open task to modify")
        action = str(args.get("action", "")).strip()
        step_num = args.get("step")
        desc = str(args.get("description", "")).strip() or None
        after_step = args.get("after_step")

        async with ctx.pool.acquire() as conn, conn.transaction():
            if action == "add":
                if not desc:
                    return ToolResult(ok=False, display="need description",
                                      error="description is required for add")
                # Determine seq for new step
                if after_step is not None:
                    new_seq = int(after_step) + 1
                    # Shift existing steps after the insertion point up by one, COLLISION-FREE. A single
                    # `seq = seq + 1` violates UNIQUE(task_id,seq) transiently (3→4 while 4 exists), and
                    # `ORDER BY` is not valid on UPDATE in PostgreSQL (Final audit) — so move the affected
                    # rows into the negative domain, then back as +1 (both statements are collision-free
                    # and happen inside this transaction, so intermediate negatives are never visible).
                    await conn.execute(
                        "UPDATE task_step SET seq = -seq WHERE task_id = $1 AND seq > $2",
                        task.id, int(after_step))
                    await conn.execute(
                        "UPDATE task_step SET seq = (-seq) + 1 WHERE task_id = $1 AND seq < 0",
                        task.id)
                else:
                    max_seq = await conn.fetchval(
                        "SELECT coalesce(max(seq), 0) FROM task_step WHERE task_id = $1",
                        task.id)
                    new_seq = max_seq + 1
                await conn.execute(
                    "INSERT INTO task_step (task_id, seq, description) VALUES ($1, $2, $3)",
                    task.id, new_seq, desc)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "add", "step": new_seq, "description": desc})
                return ToolResult(
                    ok=True,
                    output={"action": "add", "step": new_seq, "description": desc},
                    display=f"added step {new_seq}: {desc}")

            elif action == "remove":
                if step_num is None:
                    return ToolResult(ok=False, display="need step",
                                      error="step number is required for remove")
                step_num = int(step_num)
                await conn.execute(
                    "DELETE FROM task_step WHERE task_id = $1 AND seq = $2",
                    task.id, step_num)
                # Renumber remaining steps to fill the gap
                await conn.execute(
                    "UPDATE task_step SET seq = seq - 1 WHERE task_id = $1 AND seq > $2",
                    task.id, step_num)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "remove", "step": step_num})
                return ToolResult(
                    ok=True,
                    output={"action": "remove", "step": step_num},
                    display=f"removed step {step_num}")

            elif action == "replace":
                if step_num is None or not desc:
                    return ToolResult(ok=False, display="need step + description",
                                      error="step and description are required for replace")
                step_num = int(step_num)
                await conn.execute(
                    "UPDATE task_step SET description = $3, status = 'pending', "
                    "  attempts = 0, last_error = NULL, failure_class = NULL, "
                    "  checkpoint = NULL "
                    "WHERE task_id = $1 AND seq = $2",
                    task.id, step_num, desc)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "replace", "step": step_num, "description": desc})
                return ToolResult(
                    ok=True,
                    output={"action": "replace", "step": step_num, "description": desc},
                    display=f"replaced step {step_num}: {desc}")

            elif action == "reset":
                if step_num is None:
                    return ToolResult(ok=False, display="need step",
                                      error="step number is required for reset")
                step_num = int(step_num)
                await conn.execute(
                    "UPDATE task_step SET status = 'pending', attempts = 0, "
                    "  last_error = NULL, failure_class = NULL, checkpoint = NULL, "
                    "  verified = false, verified_by = NULL "
                    "WHERE task_id = $1 AND seq = $2",
                    task.id, step_num)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "reset", "step": step_num})
                return ToolResult(
                    ok=True,
                    output={"action": "reset", "step": step_num},
                    display=f"reset step {step_num} to pending")

            else:
                return ToolResult(ok=False, display="bad action",
                                  error=f"unknown action '{action}' — use add/remove/replace/reset")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (PlanTask(), AdvanceTask(), FinishTask(), ReviewTask(), ConfirmTask(), ModifyTask()):
        registry.register(tool)
