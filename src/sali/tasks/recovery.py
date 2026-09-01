"""Task recovery — detect orphaned tasks and resume from durable checkpoints.

After a crash or disconnection, tasks left in 'running' state must be recovered
deterministically. The database is authoritative — not the LLM's memory.

Recovery algorithm:
1. Find tasks with status='running' and stale heartbeat (>2 min old).
2. For each: determine if the last execution was idempotent.
3. If idempotent or no execution in flight → mark interrupted, safe to resume.
4. If non-idempotent → verify against reality before deciding.
5. Never blindly re-run completed steps.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.task_recovery")

# A task whose heartbeat is older than this is considered orphaned.
_STALE_HEARTBEAT = timedelta(minutes=2)


async def detect_orphaned_tasks(pool: Any) -> list[dict[str, Any]]:
    """Find tasks that were running when the process disappeared.

    Returns a list of task summaries with recovery recommendations.
    Does NOT modify any state — the caller decides what to do.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.id, t.objective, t.status, t.workspace_root, t.last_heartbeat, "
            "  t.is_primary, t.retry_count, t.max_retries, "
            "  (SELECT count(*) FROM task_step WHERE task_id=t.id AND status='done') AS done_steps, "
            "  (SELECT count(*) FROM task_step WHERE task_id=t.id) AS total_steps "
            "FROM task t "
            "WHERE t.status = 'running' "
            "  AND t.last_heartbeat IS NOT NULL "
            "  AND t.last_heartbeat < now() - interval '2 minutes' "
            "ORDER BY t.last_heartbeat DESC")
        results = []
        for row in rows:
            # Check if there's an interrupted execution in flight
            exec_row = await conn.fetchrow(
                "SELECT id, tool_name, status, idempotent, attempt "
                "FROM task_execution "
                "WHERE task_id=$1 AND status='running' "
                "ORDER BY started_at DESC LIMIT 1", row["id"])
            results.append({
                "task_id": str(row["id"]),
                "objective": row["objective"],
                "workspace_root": row["workspace_root"],
                "last_heartbeat": row["last_heartbeat"].isoformat() if row["last_heartbeat"] else None,
                "done_steps": row["done_steps"],
                "total_steps": row["total_steps"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "interrupted_execution": {
                    "id": str(exec_row["id"]),
                    "tool_name": exec_row["tool_name"],
                    "idempotent": exec_row["idempotent"],
                    "attempt": exec_row["attempt"],
                } if exec_row else None,
                "can_resume": row["retry_count"] < row["max_retries"],
            })
    return results


async def mark_task_interrupted(
    pool: Any, task_id: UUID, *, reason: str = "process_crash",
) -> None:
    """Mark a running task as interrupted. Does NOT change its primary status.

    The task stays recoverable — it can be resumed when Sali restarts.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE task SET "
            "  interrupted_at = now(), "
            "  recovery_reason = $1, "
            "  updated_at = now() "
            "WHERE id = $2 AND status = 'running'",
            reason, task_id)
        # Mark any in-flight executions as interrupted
        await conn.execute(
            "UPDATE task_execution SET status = 'interrupted', finished_at = now() "
            "WHERE task_id = $1 AND status = 'running'", task_id)
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ('task.interrupted', 'task', $1, $2)",
            task_id, {"reason": reason})


async def recover_task(pool: Any, task_id: UUID) -> dict[str, Any]:
    """Prepare a task for resumption. Returns recovery instructions.

    Does NOT blindly re-run steps. Instead:
    - Completed steps stay completed.
    - Interrupted executions are checked for idempotency.
    - The task is marked as ready to resume (status stays 'running').
    - A recovery context is built for the LLM.
    """
    async with pool.acquire() as conn, conn.transaction():
        task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
        if task is None:
            return {"error": "task not found"}

        steps = await conn.fetch(
            "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)

        # Get the last execution state
        last_exec = await conn.fetchrow(
            "SELECT * FROM task_execution "
            "WHERE task_id = $1 ORDER BY started_at DESC LIMIT 1", task_id)

        # Get artifacts
        artifacts = await conn.fetch(
            "SELECT artifact_path, artifact_type, created_at "
            "FROM task_artifact WHERE task_id = $1 ORDER BY created_at", task_id)

        # Clear interrupted state — task is being recovered
        await conn.execute(
            "UPDATE task SET "
            "  interrupted_at = NULL, "
            "  recovery_reason = NULL, "
            "  retry_count = retry_count + 1, "
            "  last_heartbeat = now(), "
            "  updated_at = now() "
            "WHERE id = $1", task_id)

        # Build recovery context
        completed = [dict(s) for s in steps if s["status"] == "done"]
        failed = [dict(s) for s in steps if s["status"] == "failed"]
        pending = [dict(s) for s in steps if s["status"] in ("pending", "waiting", "blocked")]
        running = [dict(s) for s in steps if s["status"] == "running"]

        # Detect steps that have successful tool executions but weren't advanced to 'done'.
        # This handles the crash-between-tool-success-and-step-advancement scenario.
        orphaned_executions = []
        for step in steps:
            if step["status"] in ("pending", "running"):
                exec_row = await conn.fetchrow(
                    "SELECT id, tool_name, status, finished_at "
                    "FROM task_execution "
                    "WHERE task_id = $1 AND step_seq = $2 AND status = 'completed' "
                    "ORDER BY finished_at DESC LIMIT 1",
                    task_id, step["seq"])
                if exec_row is not None:
                    orphaned_executions.append({
                        "step_seq": step["seq"],
                        "step_status": step["status"],
                        "execution_id": str(exec_row["id"]),
                        "tool_name": exec_row["tool_name"],
                        "finished_at": str(exec_row["finished_at"]) if exec_row["finished_at"] else None,
                    })

        return {
            "task_id": str(task_id),
            "objective": task["objective"],
            "workspace_root": task["workspace_root"],
            "last_progress_at": str(task["last_progress_at"]) if task.get("last_progress_at") else None,
            "last_progress_type": task.get("last_progress_type"),
            "health_status": task.get("health_status", "healthy"),
            "status": "recovering",
            "completed_steps": len(completed),
            "failed_steps": len(failed),
            "pending_steps": len(pending),
            "running_steps": len(running),
            "total_steps": len(steps),
            "retry_count": task["retry_count"] + 1,
            "max_retries": task["max_retries"],
            "last_execution": {
                "tool_name": last_exec["tool_name"],
                "status": last_exec["status"],
                "idempotent": last_exec["idempotent"],
                "error": last_exec["error"],
            } if last_exec else None,
            "orphaned_executions": orphaned_executions,
            "artifacts": [dict(a) for a in artifacts],
            "steps": [dict(s) for s in steps],
        }


async def write_heartbeat(pool: Any, task_id: UUID) -> None:
    """Update the heartbeat timestamp for a running task.

    Called periodically during task execution to prove the task is alive.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_heartbeat = now() WHERE id = $1 AND status = 'running'",
            task_id)


def build_recovery_context_block(recovery: dict[str, Any]) -> str:
    """Build a deterministic context block from recovery state.

    This replaces the LLM's memory as the authoritative source of task state.
    """
    lines = [
        "TASK RECOVERY — resuming from durable state",
        f"OBJECTIVE: {recovery['objective']}",
        f"TASK ID: {recovery['task_id']}",
        f"RETRY: {recovery['retry_count']}/{recovery['max_retries']}",
    ]
    if recovery.get("workspace_root"):
        lines.append(f"WORKSPACE: {recovery['workspace_root']}")
    if recovery.get("health_status") and recovery["health_status"] != "healthy":
        lines.append(f"HEALTH: {recovery['health_status']}")
    if recovery.get("last_progress_at"):
        lines.append(f"LAST PROGRESS: {recovery['last_progress_at']} ({recovery.get('last_progress_type', 'unknown')})")

    lines.append(f"\nPROGRESS: {recovery['completed_steps']}/{recovery['total_steps']} steps done")

    if recovery.get("failed_steps"):
        lines.append(f"FAILED STEPS: {recovery['failed_steps']}")

    # Show step details
    lines.append("\nSTEPS:")
    for step in recovery.get("steps", []):
        status = step["status"]
        mark = {"done": "✓", "failed": "✗", "running": "▷", "pending": "·",
                "waiting": "⋯", "blocked": "⊘", "skipped": "–"}.get(status, "?")
        line = f"  {step['seq']}. [{mark}] {step['description']}"
        if step.get("checkpoint"):
            cp = step["checkpoint"]
            line += f" [checkpoint: {', '.join(f'{k}={v}' for k, v in list(cp.items())[:3])}]"
        if step.get("last_error"):
            line += f" [error: {step['last_error'][:80]}]"
        lines.append(line)

    if recovery.get("last_execution"):
        le = recovery["last_execution"]
        lines.append(f"\nLAST EXECUTION: {le['tool_name']} → {le['status']}")
        if le.get("error"):
            lines.append(f"  error: {le['error'][:120]}")

    if recovery.get("orphaned_executions"):
        lines.append("\nORPHANED EXECUTIONS (tool succeeded but step not advanced):")
        for oe in recovery["orphaned_executions"]:
            lines.append(
                f"  step {oe['step_seq']}: {oe['tool_name']} completed "
                f"(execution {oe['execution_id'][:8]}) — step still {oe['step_status']}. "
                f"Use advance_task(step={oe['step_seq']}, status='done') to mark it done."
            )

    if recovery.get("artifacts"):
        lines.append(f"\nARTIFACTS ({len(recovery['artifacts'])}):")
        for a in recovery["artifacts"][:10]:
            lines.append(f"  - {a['artifact_path']} ({a['artifact_type']})")

    lines.append("\nIMPORTANT: Do NOT re-run completed steps. Resume from the next pending step.")
    lines.append("Verify existing artifacts before overwriting. The filesystem is the source of truth.")

    return "\n".join(lines)
