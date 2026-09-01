"""TaskStore — persist and advance multi-step tasks (spec §24).

A task and its steps live in the datastore, so they survive process restarts: the loop reads the
open tasks back into context each turn and Sali resumes. The store keeps the task's own status in
sync with its steps (all steps done → the task is done), so "is this finished?" is never guessed.
"""

from __future__ import annotations

import contextlib
from typing import Any, cast
from uuid import UUID

from sali.core.errors import classify_failure
from sali.tasks.models import Task, row_to_task

_OPEN = ("open", "running", "waiting", "blocked", "paused")


class TaskStore:
    def __init__(self, pool: Any, publisher: Any = None, reviewer: Any = None) -> None:
        self.pool = pool
        self._publisher = publisher  # EventPublisher — set by runtime
        # TaskReviewer — the completion gate (Prompt 4). Injected by the runtime/kernel (like the
        # publisher); None in a bare store (tests, API reads) → the pre-review completion behaviour.
        self._reviewer = reviewer
        # Experience-extraction hook (Prompt: lifetime memory §18/§19). An async callable(task_id) the
        # runtime injects; the store calls it on a PASS *before* archiving/cleaning the workspace, so the
        # distilled durable experience is captured while the task graph still exists. None in a bare
        # store. The store (tasks layer) never imports the learning layer — it just invokes the hook.
        self._experience_hook: Any = None

    async def _review_gate(self, task_id: UUID, run_id: UUID | None) -> bool:
        """The completion authority: True only when durable evidence proves the task complete (§1/§8).
        When a reviewer is wired, a FRESH review is run against the current durable state (never a
        possibly-stale earlier pass — an artifact could have been deleted since), and only a PASS
        permits the transition; a non-pass emits task.completion_rejected and blocks it. No reviewer
        wired (bare store) → completion is permitted as before."""
        if self._reviewer is None:
            return True
        review = await self._reviewer.review(task_id, run_id=run_id)
        if review.is_pass:
            return True
        with contextlib.suppress(Exception):
            if self._publisher is not None:
                await self._publisher.emit(
                    event_type="task.completion_rejected", task_id=task_id, run_id=run_id,
                    subject_type="task", subject_id=task_id, origin="reviewer",
                    data={"review_id": str(review.review_id), "review_status": review.status.value,
                          "reason": "review_required", "summary": review.summary,
                          "failures": review.failures[:10]})
        return False

    async def create(
        self, objective: str, steps: list[Any], *, session_id: UUID | None = None,
        workspace_root: str | None = None, allowed_write_roots: list[str] | None = None,
        workspace_mode: str = "none", **_: Any,
    ) -> Task:
        """Record a new multi-step task (status 'open'). Steps are numbered 1..N. Each step is either a
        plain description string, or a {"description", "depends_on": [seq,…]} dict for a per-step DAG (§3-6)."""
        async with self.pool.acquire() as conn, conn.transaction():
            task = await conn.fetchrow(
                "INSERT INTO task (session_id, objective, workspace_root, allowed_write_roots, workspace_mode) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                session_id, objective, workspace_root, allowed_write_roots or [], workspace_mode)
            step_rows = []
            for i, spec in enumerate(steps, start=1):
                if isinstance(spec, dict):
                    desc = str(spec.get("description", ""))
                    deps = [int(d) for d in (spec.get("depends_on") or [])]
                else:
                    desc, deps = str(spec), []
                step_rows.append(await conn.fetchrow(
                    "INSERT INTO task_step (task_id, seq, description, depends_on) "
                    "VALUES ($1, $2, $3, $4) RETURNING *",
                    task["id"], i, desc, deps))
            await _emit_task(conn, "task.created", task["id"],
                             {"objective": objective, "steps": len(step_rows)},
                             publisher=self._publisher)
        return row_to_task(task, step_rows)

    async def set_workspace(
        self, task_id: UUID, workspace_root: str, allowed_write_roots: list[str], mode: str,
    ) -> None:
        """Bind a task to its authoritative workspace (Prompt 5). Durable — the workspace becomes task
        state that survives compaction/interruption/restart and is NEVER re-resolved on continuation."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task SET workspace_root = $1, allowed_write_roots = $2, workspace_mode = $3, "
                "  updated_at = now() WHERE id = $4",
                workspace_root, allowed_write_roots, mode, task_id)

    async def _verify_learning_candidates(self, task_id: UUID) -> None:
        """When a task completes with a reviewer PASS, its learning candidates become VERIFIED and rise to
        the strongest evidence level (Prompt 7 §4 level 5 = reviewer-verified) — now, and only now,
        promotable to durable memory (Prompt 5 §18/§19). A task that never passes leaves them below the
        bar, so a failed experiment can never become knowledge. Evidence level 5 is written literally to
        keep the tasks layer free of a learning-layer import."""
        with contextlib.suppress(Exception):
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE learning_candidate SET verification_state = 'verified', "
                    "  evidence_level = greatest(evidence_level, 5), "
                    "  confidence = greatest(confidence, 0.9), updated_at = now() "
                    "WHERE task_id = $1 AND verification_state IN "
                    "  ('unverified', 'observed', 'attempted', 'supported')", task_id)

    async def _extract_experience(self, task_id: UUID) -> None:
        """Fire the injected experience-extraction hook on a PASS, BEFORE the workspace/task graph is
        archived and cleaned (Prompt: lifetime memory §18/§19). Best-effort: extracting durable
        experience must never block or fail task completion. No-op in a bare store (no hook wired)."""
        if self._experience_hook is None:
            return
        with contextlib.suppress(Exception):
            await self._experience_hook(task_id)

    async def _cleanup_workspace(self, task_id: UUID) -> None:
        """On a verified completion, clean up an EPHEMERAL task workspace — AFTER experience extraction
        and BEFORE the task row is archived/deleted (§24). User-owned workspaces are never auto-deleted
        (§25); a failure is recorded as durable, resumable pending state, never fatal (§26). Same-layer
        call (the cleanup store lives in the tasks layer) — no injected hook needed."""
        with contextlib.suppress(Exception):
            from sali.tasks.cleanup import WorkspaceCleanupStore
            await WorkspaceCleanupStore(self.pool, self._publisher).cleanup_task(task_id)

    async def bind_workspace(
        self, task_id: UUID, *, objective: str, explicit: str | None,
        sali_works_root: str, cwd: str | None = None,
    ) -> dict[str, Any]:
        """Resolve and persist a task's authoritative workspace by strict priority (§1/§4), then emit
        workspace.resolved (and workspace.rejected if a user-referenced root was refused as unsafe, §3).
        An existing task workspace is honoured, never re-resolved. Called once, at task creation."""
        from sali.tasks.workspace import resolve_task_workspace
        current = await self.get(task_id)
        existing = current.workspace_root if current is not None else None
        res = resolve_task_workspace(
            objective=objective, explicit=explicit, active_task_workspace=existing,
            sali_works_root=sali_works_root, task_id=task_id, cwd=cwd)
        root = str(res.workspace.workspace_root)
        await self.set_workspace(
            task_id, root, [str(r) for r in res.workspace.allowed_write_roots], res.mode)
        if self._publisher is not None:
            with contextlib.suppress(Exception):
                if res.rejected:
                    await self._publisher.emit(
                        event_type="workspace.rejected", task_id=task_id, subject_type="task",
                        subject_id=task_id, origin="workspace",
                        data={"rejected": res.rejected, "reason": "unsafe_root", "fell_back_to": root})
                await self._publisher.emit(
                    event_type="workspace.resolved", task_id=task_id, subject_type="task",
                    subject_id=task_id, origin="workspace",
                    data={"workspace": root, "mode": res.mode})
        return {"workspace_root": root, "mode": res.mode, "rejected": res.rejected}

    async def checkpoint(self, task_id: UUID, step_seq: int, data: dict[str, Any]) -> None:
        """Save in-step progress so a long step resumes MID-step after a crash (§5), never from scratch.
        Marks the step 'running' and stamps started_at so the resume note shows work is under way.
        Also records meaningful progress for the watchdog."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_step SET checkpoint = $1, "
                "  status = CASE WHEN status IN ('pending','waiting') THEN 'running' ELSE status END, "
                "  started_at = coalesce(started_at, now()) "
                "WHERE task_id = $2 AND seq = $3",
                data, task_id, step_seq)
            await self._record_progress(conn, task_id, "checkpoint")

    async def record_progress(
        self, task_id: UUID, progress_type: str, *, tool_name: str | None = None,
    ) -> None:
        """Record meaningful progress on a task. Called on step advance, tool success,
        artifact creation, checkpoint — NOT on heartbeat or LLM prose."""
        async with self.pool.acquire() as conn:
            await self._record_progress(conn, task_id, progress_type, tool_name=tool_name)

    async def _record_progress(
        self, conn: Any, task_id: UUID, progress_type: str, *, tool_name: str | None = None,
    ) -> None:
        """Internal: update last_progress_at and health_status on the task.
        Also emits a task.progress event for observers."""
        await conn.execute(
            "UPDATE task SET "
            "  last_progress_at = now(), "
            "  last_progress_type = $2, "
            "  health_status = 'healthy', "
            "  updated_at = now() "
            "WHERE id = $1 AND status = 'running'",
            task_id, progress_type)
        # Emit progress event for observers (API, iOS, terminal)
        await _emit_task(conn, "task.progress", task_id,
                         {"progress_type": progress_type, "tool_name": tool_name},
                         publisher=self._publisher)

    async def set_active_tool(self, task_id: UUID, tool_name: str | None) -> None:
        """Track which tool is currently executing (for watchdog context)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task SET active_tool_name = $2, updated_at = now() "
                "WHERE id = $1 AND status = 'running'",
                task_id, tool_name)

    async def get(self, task_id: UUID) -> Task | None:
        async with self.pool.acquire() as conn:
            task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            if task is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
        return row_to_task(task, steps)

    async def open_tasks(self, *, limit: int = 5) -> list[Task]:
        """The tasks still worth resuming, most-recently-touched first."""
        async with self.pool.acquire() as conn:
            tasks = await conn.fetch(
                "SELECT * FROM task WHERE status IN ('open', 'running', 'waiting', 'blocked', 'paused') "
                "ORDER BY updated_at DESC LIMIT $1", limit)
            out = []
            for t in tasks:
                steps = await conn.fetch(
                    "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", t["id"])
                out.append(row_to_task(t, steps))
        return out

    async def current(self) -> Task | None:
        """The single task Sali is most likely working on (the most-recently-touched open one)."""
        tasks = await self.open_tasks(limit=1)
        return tasks[0] if tasks else None

    async def advance(
        self,
        task_id: UUID,
        step_seq: int,
        status: str,
        *,
        note: str | None = None,
        error: str | None = None,
        verified_by: UUID | None = None,
        run_id: UUID | None = None,
    ) -> tuple[Task | None, str | None]:
        """Set a step's status and re-derive the task's: all steps done/skipped → the task is done,
        otherwise it's running.

        Returns (task_or_None, error_message). If validation fails, returns (None, error_message)
        without modifying any state. If the task auto-completes and is archived, returns (None, None).

        Validation:
        1. step_seq must exist in the task
        2. Dependencies must be satisfied before marking done
        3. 'done' requires verified_by when the step has tool executions (evidence gate)

        On a 'failed' step, records an evidence trail the retry driver reads (§8/§9).
        On a 'done' step, ``verified_by`` links the tool_execution that verified the effect."""
        err_text = error if error is not None else (note if status == "failed" else None)
        failure_class = classify_failure(err_text).value if status == "failed" else None
        async with self.pool.acquire() as conn, conn.transaction():
            # 1. Validate step exists
            step_row = await conn.fetchrow(
                "SELECT seq, status, depends_on FROM task_step WHERE task_id = $1 AND seq = $2",
                task_id, step_seq)
            if step_row is None:
                return None, f"step {step_seq} does not exist in this task"

            # 2. Validate dependencies are satisfied before marking done
            if status == "done":
                depends_on = list(step_row["depends_on"] or [])
                if depends_on:
                    dep_statuses = await conn.fetch(
                        "SELECT seq, status FROM task_step "
                        "WHERE task_id = $1 AND seq = ANY($2)",
                        task_id, depends_on)
                    dep_map = {r["seq"]: r["status"] for r in dep_statuses}
                    unsatisfied = [
                        d for d in depends_on
                        if dep_map.get(d) not in ("done", "skipped")
                    ]
                    if unsatisfied:
                        return None, (
                            f"step {step_seq} cannot be marked done: "
                            f"dependencies not satisfied: {unsatisfied}"
                        )

                # 3. Evidence gate: 'done' requires verified_by when tool executions exist
                #    Exception: steps with no tool executions (pure planning/observation) can
                #    be marked done without verification.
                has_executions = await conn.fetchval(
                    "SELECT count(*) FROM task_execution "
                    "WHERE task_id = $1 AND step_seq = $2 AND status = 'completed'",
                    task_id, step_seq)
                if has_executions > 0 and verified_by is None:
                    return None, (
                        f"step {step_seq} has tool executions but no verified evidence. "
                        f"Provide verified_by (a successful tool_execution id) or mark the step "
                        f"as 'running' and continue working."
                    )

            # All validations passed — apply the update
            await conn.execute(
                "UPDATE task_step SET status = $1, note = coalesce($2, note), "
                "  started_at = coalesce(started_at, "
                "    CASE WHEN $1 IN ('running','done','failed') THEN now() END), "
                "  finished_at = CASE WHEN $1 IN ('done','failed','skipped') THEN now() "
                "    ELSE finished_at END, "
                "  attempts = attempts + CASE WHEN $1 = 'failed' THEN 1 ELSE 0 END, "
                "  last_error = CASE WHEN $1 = 'failed' THEN $5 ELSE last_error END, "
                "  failure_class = CASE WHEN $1 = 'failed' THEN $6::task_failure_class ELSE failure_class END, "
                "  verified_by = coalesce($7, verified_by), "
                "  verified = CASE WHEN $1 = 'done' AND $7 IS NOT NULL THEN true ELSE verified END "
                "WHERE task_id = $3 AND seq = $4",
                status, note, task_id, step_seq, err_text, failure_class, verified_by)
            statuses = [r["status"] for r in await conn.fetch(
                "SELECT status FROM task_step WHERE task_id = $1", task_id)]
            all_complete = bool(statuses) and all(s in ("done", "skipped") for s in statuses)
            # When a reviewer gates completion (Prompt 4), DON'T flip to 'done' inside this transaction:
            # the review must first observe the just-updated steps (run after commit), and completion is
            # permitted only on PASS. Without a reviewer (bare store), keep the original auto-complete.
            inline_done = all_complete and self._reviewer is None
            task_status = "done" if inline_done else "running"
            await conn.execute(
                "UPDATE task SET status = $1, is_primary = CASE WHEN $1 = 'done' THEN false ELSE is_primary END, "
                "  last_progress_at = now(), last_progress_type = 'step_advance', "
                "  health_status = 'healthy', updated_at = now() "
                "WHERE id = $2 AND status IN ('open', 'running')",
                task_status, task_id)
            await _emit_task(conn, "task.step_advanced", task_id,
                             {"seq": step_seq, "status": status, "task_status": task_status},
                             publisher=self._publisher)
            # Emit progress event for observers
            await _emit_task(conn, "task.progress", task_id,
                             {"progress_type": "step_advance", "step_seq": step_seq},
                             publisher=self._publisher)
            # Emit specific step lifecycle events for observers
            if status == "done":
                await _emit_task(conn, "task.step.completed", task_id,
                                 {"seq": step_seq, "verified": verified_by is not None},
                                 publisher=self._publisher)
            elif status == "failed":
                await _emit_task(conn, "task.step.failed", task_id,
                                 {"seq": step_seq, "error": err_text[:200] if err_text else None},
                                 publisher=self._publisher)
        # Auto-archive when all steps complete AND no reviewer gates completion (bare store).
        if inline_done:
            with contextlib.suppress(Exception):
                await self._archive_and_cleanup(task_id)
            return (None, None)
        # Reviewer-gated completion (Prompt 4): all steps are done, but the task becomes 'done' only
        # once the reviewer verifies the durable evidence. The review runs here (after the step update
        # committed, so it sees the real state); on PASS we complete, otherwise the task stays running
        # with the review's findings durable for the rework loop (task_id unchanged, same primary task).
        if all_complete and self._reviewer is not None and await self._review_gate(task_id, run_id):
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE task SET status = 'done', is_primary = false, updated_at = now() "
                    "WHERE id = $1 AND status IN ('open', 'running')", task_id)
                await _emit_task(conn, "task.finished", task_id,
                                 {"status": "done", "via": "review"}, publisher=self._publisher)
            await self._verify_learning_candidates(task_id)  # evidence-backed by the reviewer PASS (§19)
            await self._extract_experience(task_id)  # distil durable experience BEFORE cleanup (§18/§19)
            await self._cleanup_workspace(task_id)   # delete an EPHEMERAL workspace, durably (§24-26)
            with contextlib.suppress(Exception):
                await self._archive_and_cleanup(task_id)
            return (None, None)
        # Return None if archived (task deleted from DB), otherwise return the task.
        return (await self.get(task_id), None)

    async def finish(
        self, task_id: UUID, *, status: str = "done", result: str | None = None,
        run_id: UUID | None = None,
    ) -> str | None:
        """Explicitly close a task ('done' / 'failed' / 'abandoned') with an optional result note.

        Returns None on success, or an error message if validation fails.

        For status='done': all required steps must be completed AND, when a reviewer is wired, the
        reviewer gate must PASS on the durable evidence — the model saying "done" is never enough
        (§1/§8). For 'failed'/'abandoned': no gate (an explicit stop is always allowed).

        After closing, archives the full task record to sali-works/tasks/ and cleans up DB rows.
        """
        async with self.pool.acquire() as conn:
            # For 'done', verify all steps are completed
            if status == "done":
                steps = await conn.fetch(
                    "SELECT seq, status FROM task_step WHERE task_id = $1", task_id)
                if steps:
                    incomplete = [
                        r["seq"] for r in steps
                        if r["status"] not in ("done", "skipped")
                    ]
                    if incomplete:
                        return (
                            f"cannot mark task done: {len(incomplete)} step(s) incomplete: "
                            f"{incomplete}. Complete all steps before finishing."
                        )

        # Reviewer gate (Prompt 4): durable evidence must prove completion. A passing review already
        # produced this turn (e.g. by the finish_task tool) is reused; otherwise one is run here — so a
        # direct finish('done') can NEVER bypass the reviewer. A non-pass keeps the task open for rework.
        if status == "done" and not await self._review_gate(task_id, run_id):
            return "review_required"

        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task SET status = $1, result = coalesce($2, result), "
                "  is_primary = false, updated_at = now() "
                "WHERE id = $3", status, result, task_id)
            await _emit_task(conn, "task.finished", task_id, {"status": status}, publisher=self._publisher)

        if status == "done":  # reviewer PASSED → its learning candidates are now evidence-backed (§19)
            await self._verify_learning_candidates(task_id)
            await self._extract_experience(task_id)  # distil durable experience BEFORE cleanup (§18/§19)
            await self._cleanup_workspace(task_id)   # delete an EPHEMERAL workspace, durably (§24-26)
        # Archive to sali-works/tasks/ and clean up DB rows.
        with contextlib.suppress(Exception):
            await self._archive_and_cleanup(task_id)
        return None

    async def _archive_and_cleanup(self, task_id: UUID) -> None:
        """Save final task snapshot to sali-works/tasks/ and delete DB rows.

        Order: read data → archive to filesystem → delete from DB.
        If the filesystem write fails, the task stays in the DB (safe — can retry).
        If the delete fails after archive succeeds, the task is in both places (safe — idempotent).
        """
        from sali.tasks.logger import append_event, save_task_record
        async with self.pool.acquire() as conn:
            task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            if task is None or task["status"] not in ("done", "failed", "abandoned", "cancelled"):
                return
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
            executions = await conn.fetch(
                "SELECT id, step_seq, tool_name, status, result_summary, error, "
                "  attempt, idempotent, started_at, finished_at "
                "FROM task_execution WHERE task_id = $1 ORDER BY started_at", task_id)
            artifacts = await conn.fetch(
                "SELECT artifact_path, artifact_type, tool_name, created_at "
                "FROM task_artifact WHERE task_id = $1 ORDER BY created_at", task_id)
            reviews = await conn.fetch(
                "SELECT review_id, run_id, attempt, status, reviewer_type, summary, "
                "  requirements_checked, failures, recommendations, passed_count, failed_count, "
                "  unknown_count, started_at, completed_at "
                "FROM task_review WHERE task_id = $1 ORDER BY attempt", task_id)

        # Archive to filesystem FIRST — if this fails, task stays in DB (safe).
        save_task_record(
            task_id, dict(task), [dict(s) for s in steps],
            executions=[dict(e) for e in executions],
            artifacts=[dict(a) for a in artifacts],
            reviews=[dict(r) for r in reviews])
        append_event(task_id, "task_archived",
                     {"status": task["status"], "objective": task["objective"]})

        # Delete DB rows AFTER successful archive. CASCADE handles child tables.
        # Clear sali_state references first (FK with ON DELETE SET NULL is the safety net,
        # but explicit clear is faster and avoids FK check overhead).
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sali_state SET active_task_id = NULL WHERE active_task_id = $1", task_id)
            await conn.execute(
                "UPDATE sali_state SET previous_task_id = NULL WHERE previous_task_id = $1", task_id)
            await conn.execute("DELETE FROM task WHERE id = $1", task_id)

    async def active_task(self) -> Task | None:
        """The single primary ACTIVE task, or None."""
        async with self.pool.acquire() as conn:
            task = await conn.fetchrow(
                "SELECT * FROM task WHERE is_primary AND status NOT IN "
                "('done','failed','abandoned','cancelled','superseded') "
                "ORDER BY updated_at DESC LIMIT 1")
            if task is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task["id"])
        return row_to_task(task, steps)

    async def activate(self, task_id: UUID) -> Task | None:
        """Mark a task as the primary active task. Clears any existing primary first."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE task SET is_primary = false WHERE is_primary")
            await conn.execute(
                "UPDATE task SET is_primary = true, status = CASE "
                "  WHEN status IN ('open','paused','waiting','blocked') THEN 'running' "
                "  ELSE status END, updated_at = now() WHERE id = $1", task_id)
            await _emit_task(conn, "task.activated", task_id, {}, publisher=self._publisher)
            task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            if task is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
        return row_to_task(task, steps)

    async def supersede(self, old_task_id: UUID, new_task_id: UUID, *, reason: str = "") -> None:
        """Supersede the old task and activate the new one.

        PRESERVES the old task's execution status (waiting, paused, running, etc.) — only
        the authority relationship changes: is_primary=false and superseded_by is set.
        The old task's execution state is meaningful history that must not be destroyed.
        A waiting task that was superseded is still 'waiting' if later resumed — it doesn't
        magically become something else."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE task SET is_primary = false, "
                "  superseded_by = $1, updated_at = now() "
                "WHERE id = $2 AND status NOT IN ('done','failed','abandoned','cancelled','superseded')",
                new_task_id, old_task_id)
            await conn.execute(
                "UPDATE task SET is_primary = true, status = CASE "
                "  WHEN status IN ('open','paused','waiting','blocked') THEN 'running' "
                "  ELSE status END, updated_at = now() WHERE id = $1", new_task_id)
            await _emit_task(conn, "task.superseded", old_task_id,
                             {"new_task": str(new_task_id), "reason": reason},
                             publisher=self._publisher)
            await _emit_task(conn, "task.activated", new_task_id,
                             {"superseded": str(old_task_id)})

    async def cancel(self, task_id: UUID, *, reason: str = "") -> None:
        """Explicitly cancel a task — status becomes 'cancelled' (not 'abandoned').
        CANCELLED = user/controller explicitly stopped. ABANDONED = left unfinished without
        explicit cancellation. The distinction matters for audit and potential resume."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE task SET status = 'cancelled', is_primary = false, "
                "  result = coalesce($1, result), updated_at = now() "
                "WHERE id = $2 AND status NOT IN ('done','failed','abandoned','cancelled','superseded')",
                f"cancelled: {reason}" if reason else None, task_id)
            await _emit_task(conn, "task.cancelled", task_id, {"reason": reason}, publisher=self._publisher)

    async def resume(self, task_id: UUID) -> Task | None:
        """Resume a superseded/paused/cancelled task. It becomes the new primary active task.

        Accepts tasks with superseded_by IS NOT NULL or status in ('paused','cancelled'),
        because supersede() preserves execution status — a superseded task might be 'running'.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE task SET is_primary = false WHERE is_primary")
            await conn.execute(
                "UPDATE task SET is_primary = true, status = 'running', "
                "  superseded_by = NULL, updated_at = now() "
                "WHERE id = $1 AND (superseded_by IS NOT NULL OR status IN ('paused','cancelled'))",
                task_id)
            await _emit_task(conn, "task.resumed", task_id, {}, publisher=self._publisher)
            task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            if task is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
        return row_to_task(task, steps)

    async def suspend(self, task_id: UUID, *, reason: str = "") -> Task | None:
        """Suspend the primary task for an interruption (Prompt 1): mark it 'paused' and stamp
        interrupted_at, so it can be resumed EXACTLY where it stopped — from durable state (its steps,
        checkpoints, task_executions and progress are already persistent). Keeps is_primary so it stays
        THE focus; resume() re-activates it. A no-op on a finished/cancelled/superseded task."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE task SET status = 'paused', interrupted_at = now(), "
                "  recovery_reason = coalesce($1, recovery_reason), updated_at = now() "
                "WHERE id = $2 AND status NOT IN ('done','failed','abandoned','cancelled','superseded')",
                f"suspended: {reason}" if reason else None, task_id)
            await _emit_task(conn, "task.suspended", task_id, {"reason": reason}, publisher=self._publisher)
        return await self.get(task_id)

    async def record_artifact(
        self, task_id: UUID, artifact_path: str, artifact_type: str,
        *, tool_name: str | None = None,
    ) -> None:
        """Record a file artifact from a successful tool execution.

        Only called after deterministic verification (tool.verify succeeded).
        Never called based on LLM claims alone.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO task_artifact (task_id, artifact_path, artifact_type, tool_name) "
                "VALUES ($1, $2, $3, $4)",
                task_id, artifact_path, artifact_type, tool_name)

    async def artifacts(self, task_id: UUID) -> list[dict[str, Any]]:
        """List artifacts recorded for a task."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, artifact_path, artifact_type, tool_name, created_at "
                "FROM task_artifact WHERE task_id = $1 ORDER BY created_at", task_id)
        return [dict(r) for r in rows]

    async def heartbeat(self, task_id: UUID) -> None:
        """Update the heartbeat timestamp for a running task.

        Called during task execution to prove the task is alive.
        A stale heartbeat (>2 min) signals the task is orphaned.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task SET last_heartbeat = now() WHERE id = $1 AND status = 'running'",
                task_id)

    async def record_execution(
        self, task_id: UUID, step_seq: int, tool_name: str,
        *, tool_args: dict[str, Any] | None = None,
        execution_id: UUID | None = None, idempotent: bool | None = None,
        attempt: int = 1,
    ) -> UUID:
        """Record a tool execution for a task step. Returns the execution record ID.

        Used for idempotency checks during recovery: if a matching execution already
        completed, recovery must not re-run it.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO task_execution "
                "  (task_id, step_seq, tool_name, tool_args, execution_id, idempotent, attempt) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (task_id, step_seq, tool_name, attempt) DO UPDATE "
                "  SET execution_id = EXCLUDED.execution_id "
                "RETURNING id",
                task_id, step_seq, tool_name, tool_args or {}, execution_id, idempotent, attempt)
        return cast("UUID", row["id"])

    async def complete_execution(
        self, exec_record_id: UUID, *, status: str = "completed",
        result_summary: str | None = None, error: str | None = None,
    ) -> None:
        """Mark an execution record as completed/failed."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_execution SET "
                "  status = $1, result_summary = $2, error = $3, finished_at = now() "
                "WHERE id = $4",
                status, result_summary, error, exec_record_id)

    async def get_execution_history(self, task_id: UUID, step_seq: int) -> list[dict[str, Any]]:
        """Get execution history for a task step, for idempotency checks."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, tool_name, status, idempotent, attempt, error, finished_at "
                "FROM task_execution "
                "WHERE task_id = $1 AND step_seq = $2 "
                "ORDER BY attempt DESC", task_id, step_seq)
        return [dict(r) for r in rows]

    async def has_completed_execution(self, task_id: UUID, step_seq: int, tool_name: str) -> bool:
        """Check if a tool call already completed successfully for this step.

        Used for idempotency: if create_file already succeeded, don't re-create.
        """
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM task_execution "
                "WHERE task_id = $1 AND step_seq = $2 AND tool_name = $3 AND status = 'completed'",
                task_id, step_seq, tool_name)
        return int(count or 0) > 0

    async def current_step(self, task_id: UUID) -> int | None:
        """The seq of the step currently being worked on (status='running'), or None.

        Used by the loop to record executions against the correct step instead of hardcoding 0.
        Returns the first running step (by seq order) since only one step runs at a time.
        """
        async with self.pool.acquire() as conn:
            return cast("int | None", await conn.fetchval(
                "SELECT seq FROM task_step "
                "WHERE task_id = $1 AND status = 'running' ORDER BY seq LIMIT 1",
                task_id))

    # ── task-folder mirror (sali-works/tasks/) — owned here so tools route through ctx.tasks, never
    #    import the tasks layer directly (keeps the layering contract intact) ───────────────────────
    async def save_meta(self, task_id: UUID, objective: str, steps: list[str], *,
                        workspace_root: str | None = None, status: str = "open") -> None:
        from sali.tasks.logger import save_task_meta
        save_task_meta(task_id, objective, steps, workspace_root=workspace_root, status=status)

    async def log_event(self, task_id: UUID, event_type: str,
                        payload: dict[str, Any] | None = None) -> None:
        from sali.tasks.logger import append_event
        append_event(task_id, event_type, payload or {})

    async def delete_folder(self, task_id: UUID) -> bool:
        from sali.tasks.logger import delete_task_folder
        return bool(delete_task_folder(task_id))


async def _emit_task(
    conn: Any, event_type: str, task_id: UUID, payload: dict[str, Any],
    *, publisher: Any = None,
) -> None:
    """Publish a task event through the canonical publisher if available, otherwise direct SQL.

    The publisher handles both durable persistence and live delivery (WebSocket, terminal).
    When called inside a transaction, the event is persisted via the publisher's own connection
    (outside the transaction) to ensure it's always durable.
    """
    if publisher is not None:
        await publisher.emit(
            event_type=event_type, task_id=task_id,
            subject_type="task", subject_id=task_id, origin="task", data=payload)
    else:
        # Fallback: direct SQL (backward compatibility for tests, etc.)
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ($1,'task',$2,$3)",
            event_type, task_id, payload)
