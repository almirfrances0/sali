"""TaskStore — persist and advance multi-step tasks (spec §24).

A task and its steps live in the datastore, so they survive process restarts: the loop reads the
open tasks back into context each turn and Sali resumes. The store keeps the task's own status in
sync with its steps (all steps done → the task is done), so "is this finished?" is never guessed.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, cast
from uuid import UUID

from sali.core.errors import classify_failure
from sali.obs.log import get_logger
from sali.tasks.models import Task, row_to_task

log = get_logger("sali.tasks.store")

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
        wired (bare store) → completion is permitted, but a WARNING is logged so we see any
        production path that fell into this bypass (Turn 3: every prod TaskStore wires the reviewer,
        so this branch is expected to fire in tests only)."""
        if self._reviewer is None:
            # Plain stdlib logging so pytest's caplog and any log-collector picks it up.
            # (structlog's stdlib routing works too, but this is guaranteed regardless of
            # how structlog is configured in the runtime.)
            import logging as _stdlogging
            _stdlogging.getLogger("sali.task_store").warning(
                "bare_task_store_gate_bypassed task_id=%s reason=%s",
                str(task_id), "no_reviewer_wired")
            return True
        with contextlib.suppress(Exception):
            from sali.learning.capability_acquisition import CapabilityAcquisitionStore

            acq = CapabilityAcquisitionStore(self.pool, self._publisher)
            live = await acq.for_task(task_id)
            if live is not None and live["status"] in ("gap_identified", "researching", "acquiring"):
                await acq.advance(UUID(str(live["id"])), "verifying", notes="reviewer gate running")
        review = await self._reviewer.review(task_id, run_id=run_id)
        # Turn 8: denormalize the review's terminal fields onto the task row so a single
        # SELECT can render the rework loop's state. Skipped for the bare-store branch above
        # because no verdict was produced. Populated on every wired-reviewer call.
        with contextlib.suppress(Exception):
            async with self.pool.acquire() as _conn:
                await _conn.execute(
                    "UPDATE task SET last_review_id = $2, last_review_status = $3, "
                    "  last_review_at = now(), last_review_attempt = $4, "
                    "  last_review_summary = $5, updated_at = now() "
                    "WHERE id = $1",
                    task_id, review.review_id, review.status.value,
                    review.attempt, (review.summary or "")[:2000])
        if review.is_pass:
            return True
        # Turn 8: flip the task's own status so observers see the rework loop instead of a
        # phantom 'running' task that isn't moving. 'blocked_by_review' distinguishes
        # review-blocked from generic 'blocked' (external block like a missing credential).
        # NEEDS_REWORK / FAILED both land as 'needs_changes' - the model has real work to
        # do to advance. The predicate guards against races that already moved the row past
        # 'running' (e.g. a concurrent cancel).
        _new_status = ("blocked_by_review" if review.status.value == "blocked"
                       else "needs_changes")
        with contextlib.suppress(Exception):
            async with self.pool.acquire() as _conn:
                # Turn 8 hardening: widen the WHERE clause to include every non-terminal
                # status. A task in paused/waiting/blocked/needs_changes when a review
                # returns non-pass must still land on the new state - otherwise iOS shows
                # stale status and the rework loop is invisible. Only terminal states
                # (done/failed/abandoned/cancelled/superseded) are protected.
                await _conn.execute(
                    "UPDATE task SET status = $2, updated_at = now() "
                    "WHERE id = $1 AND status NOT IN ("
                    "  'done','failed','abandoned','cancelled','superseded')",
                    task_id, _new_status)
        with contextlib.suppress(Exception):
            if self._publisher is not None:
                await self._publisher.emit(
                    event_type="task.completion_rejected", task_id=task_id, run_id=run_id,
                    subject_type="task", subject_id=task_id, origin="reviewer",
                    data={"review_id": str(review.review_id), "review_status": review.status.value,
                          "task_status": _new_status,
                          "reason": "review_required", "summary": review.summary,
                          "failures": review.failures[:10]})
        return False

    async def create(
        self, objective: str, steps: list[Any], *, session_id: UUID | None = None,
        workspace_root: str | None = None, allowed_write_roots: list[str] | None = None,
        workspace_mode: str = "none", parent_task_id: UUID | None = None, **_: Any,
    ) -> Task:
        """Record a new multi-step task (status 'open'). Steps are numbered 1..N. Each step is either a
        plain description string, or a {"description", "depends_on": [seq,…]} dict for a per-step DAG (§3-6).

        Turn 4: when `parent_task_id` is set, the child inherits the parent's workspace_root,
        allowed_write_roots and workspace_mode UNLESS the caller passed explicit values. Follow-up
        tasks then can modify the same files the parent worked on ("make the header smaller" after
        the landing-page task finished) without a fresh workspace resolution round-trip.
        """
        if parent_task_id is not None and (workspace_root is None and not allowed_write_roots):
            async with self.pool.acquire() as conn:
                parent = await conn.fetchrow(
                    "SELECT workspace_root, allowed_write_roots, workspace_mode "
                    "FROM task WHERE id = $1", parent_task_id)
            if parent is not None:
                workspace_root = workspace_root or parent["workspace_root"]
                allowed_write_roots = allowed_write_roots or list(
                    parent["allowed_write_roots"] or [])
                if workspace_mode == "none":
                    workspace_mode = str(parent["workspace_mode"] or "none")
        async with self.pool.acquire() as conn, conn.transaction():
            task = await conn.fetchrow(
                "INSERT INTO task (session_id, objective, workspace_root, allowed_write_roots, "
                "  workspace_mode, parent_task_id) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                session_id, objective, workspace_root, allowed_write_roots or [], workspace_mode,
                parent_task_id)
            # SUB-STEPS (migration 0045). A spec may carry {"substeps": [...]}; each becomes its own
            # row with `parent_seq` pointing at the parent's seq. Flattened into one ordered seq space
            # so every existing query, dependency array and (task_id, seq) identity keeps working —
            # the hierarchy is expressed purely by parent_seq, which is NULL for a top-level step.
            step_rows = []
            seq = 0

            async def _insert(spec: Any, parent: int | None) -> None:
                nonlocal seq
                # STEP-DISCIPLINE (Phase A): a step spec may carry definition_of_done + scope_excludes
                # so each step becomes an enforceable CONTRACT, not a bare description the model
                # self-reports against. Both are optional/nullable — a plain-string step still works,
                # and a NULL DoD falls back to the prior trust-the-model behaviour for that step.
                if isinstance(spec, dict):
                    desc = str(spec.get("description", "") or spec.get("step", ""))
                    deps = [int(d) for d in (spec.get("depends_on") or [])]
                    subs = list(spec.get("substeps") or spec.get("sub_steps") or [])
                    dod = spec.get("definition_of_done") or spec.get("done_when")
                    excl = spec.get("scope_excludes") or spec.get("excludes")
                    dod = str(dod).strip()[:600] if dod else None
                    excl = str(excl).strip()[:600] if excl else None
                else:
                    desc, deps, subs, dod, excl = str(spec), [], [], None, None
                if not desc.strip():
                    return
                seq += 1
                mine = seq
                step_rows.append(await conn.fetchrow(
                    "INSERT INTO task_step "
                    "(task_id, seq, description, depends_on, parent_seq, "
                    " definition_of_done, scope_excludes) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                    task["id"], mine, desc, deps, parent, dod, excl))
                for sub in subs[:12]:          # a bounded depth-1 nesting; deeper plans are noise
                    if parent is None:
                        await _insert(sub, mine)

            for spec in steps:
                await _insert(spec, None)
            await _emit_task(conn, "task.created", task["id"],
                             {"objective": objective, "steps": len(step_rows)},
                             publisher=self._publisher)
        # A new objective is the moment to ask whether Sali can already do this (§7/§10/§16).
        with contextlib.suppress(Exception):
            await self._open_capability_gap(task["id"], objective)
        return row_to_task(task, step_rows)

    async def _open_capability_gap(self, task_id: UUID, objective: str) -> None:
        """Open a capability gap for a task, unless Sali already has a usable capability for it.

        This is what stops him relearning what he already knows: `already_have` is asked BEFORE any
        acquisition is opened. When there is a genuine gap it is recorded NOW, before the work — so the
        acquisition arc is a real timeline of this task rather than a label applied afterwards. The
        reusable skill has no name yet (naming it is one inference, made once the work has succeeded),
        so the objective stands in as the handle and the row is renamed at the moment it settles."""
        from sali.learning.capability import CapabilityStore
        from sali.learning.capability_acquisition import CapabilityAcquisitionStore

        caps = CapabilityStore(self.pool, self._publisher)
        for existing in await caps.discover_for_task(objective, limit=3):
            if await caps.already_have(name=existing["name"], scope_ref=existing.get("scope_ref")):
                return
        handle = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (objective or "").lower())).strip()
        if not handle:
            return
        await CapabilityAcquisitionStore(self.pool, self._publisher).identify_gap(
            capability_name=handle[:120], task_id=task_id, notes=objective[:400])

    async def _close_capability_gap(self, task_id: UUID, reason: str) -> None:
        """Never let an acquisition outlive the work that opened it.

        `open()` is the set a restart resumes (§62). A gap whose task was cancelled, superseded, or
        finished without ever yielding a reusable capability has nothing left driving it, so it settles
        here — otherwise Sali wakes up forever trying to acquire something no work is behind. The
        acquisition failing is not the task failing: a task can succeed and still teach no reusable
        skill, and saying so is more honest than leaving the gap open."""
        with contextlib.suppress(Exception):
            from sali.learning.capability_acquisition import CapabilityAcquisitionStore

            acq = CapabilityAcquisitionStore(self.pool, self._publisher)
            live = await acq.for_task(task_id)
            if live is not None:
                await acq.fail(UUID(str(live["id"])), error=reason[:200])

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

    async def _extract_experience(self, task_id: UUID) -> bool:
        """Fire the injected experience-extraction hook on a PASS, BEFORE the workspace/task graph is
        archived and cleaned (Prompt: lifetime memory §18/§19). No-op in a bare store (no hook wired).

        Returns whether the lesson is safely out. It still never *fails* task completion — but the caller
        must know, because it is about to destroy the only evidence.

        This used to swallow the exception and return None, and the caller then deleted the workspace and
        CASCADE-deleted the whole task graph regardless. Driven in a scratch database with a raising hook:
        `task`, `task_execution`, `task_step`, `task_decision` and `task_research` all went, along with the
        workspace — so a task's lesson AND every trace needed to reconstruct it were destroyed together,
        silently and irreversibly. The task filesystem is meant to be temporary; the memory is not, and
        cleanup must never be the thing that decides which.
        """
        if self._experience_hook is None:
            # Turn 3: bare store has no lesson to extract. Return False so the caller
            # preserves the workspace (nothing was learned; we cannot clean up on the
            # promise of a lesson that never landed). In production the hook is always
            # wired by the runtime, so this branch is a test-only path today.
            return False
        try:
            await self._experience_hook(task_id)
        except Exception as exc:  # noqa: BLE001 - never fatal to completion, but never silent either
            with contextlib.suppress(Exception):
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                        "VALUES ('task.experience_extraction_failed','task',$1,$2)",
                        task_id, {"error": str(exc)[:400]},
                    )
            log.warning("experience_extraction_failed", task_id=str(task_id), error=str(exc)[:200])
            return False
        return True

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

    async def get(self, task_id: UUID, *, include_archived: bool = False) -> Task | None:
        """Fetch one task by id.

        `include_archived=False` (default) hides rows whose archive step has run - this
        preserves the pre-Turn-1 contract for every operational caller, since the row
        used to disappear entirely at archive time. Pass True from §15 follow-up
        detection, history browsing, or any code path that legitimately wants a look at
        completed work.
        """
        async with self.pool.acquire() as conn:
            if include_archived:
                task = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
            else:
                task = await conn.fetchrow(
                    "SELECT * FROM task WHERE id = $1 AND archived_at IS NULL", task_id)
            if task is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
        return row_to_task(task, steps)

    async def recent_completed(self, *, limit: int = 20) -> list[Task]:
        """Archived tasks in reverse chronological order - the query §15 follow-up
        detection will use to match a new user message against tasks Sali just finished.
        Bounded so we never pull the whole history.

        Turn 4 review-hardening: filter to status='done' only. A follow-up on a
        cancelled/failed/abandoned parent is not a continuation - it's a resurrection of
        work Almir already called off, and the FOLLOWUP branch would silently rebuild it."""
        async with self.pool.acquire() as conn:
            tasks = await conn.fetch(
                "SELECT * FROM task WHERE archived_at IS NOT NULL AND status = 'done' "
                "ORDER BY archived_at DESC LIMIT $1", limit)
            out: list[Task] = []
            for t in tasks:
                steps = await conn.fetch(
                    "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", t["id"])
                out.append(row_to_task(t, steps))
        return out

    async def rename_objective(self, task_id: UUID, objective: str) -> None:
        """Turn 6: update the task's objective (Almir renaming the running task via modify_task
        change_objective). Leaves status, is_primary, steps, and execution history untouched -
        only the human-readable label changes. Emits task.modified so observers see the rename."""
        obj = (objective or "").strip()[:2000]
        if not obj:
            return
        async with self.pool.acquire() as conn:
            # Turn 6 hardening: skip archived rows (Turn 1 preserved them; a rename on an
            # archived task is meaningless and should not silently emit task.modified).
            res = await conn.execute(
                "UPDATE task SET objective = $2, updated_at = now() "
                "WHERE id = $1 AND archived_at IS NULL", task_id, obj)
            if res == "UPDATE 0":
                return   # nothing to rename (archived / missing) - no emit, no side effect
            await _emit_task(conn, "task.modified", task_id,
                             {"action": "change_objective", "objective": obj},
                             publisher=self._publisher)

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
            # Turn 7: ONE tally, one predicate - the auto-complete decision, the event
            # payloads, and the model's context all read the same numbers from the same query.
            tally = await _compute_tally(conn, task_id)
            all_complete = tally["task_status"] == "done"
            # When a reviewer gates completion (Prompt 4), DON'T flip to 'done' inline: the review
            # must first observe the just-updated steps and PASS. Without a reviewer (bare store),
            # keep the original auto-complete.
            inline_done = all_complete and self._reviewer is None
            task_status = "done" if inline_done else "running"
            await conn.execute(
                "UPDATE task SET status = $1, is_primary = CASE WHEN $1 = 'done' THEN false ELSE is_primary END, "
                "  last_progress_at = now(), last_progress_type = 'step_advance', "
                # WHEN WORK ACTUALLY BEGAN, stamped once and never moved. created_at is when the task
                # was written down; a task can sit queued behind another for an hour before anything
                # happens, so measuring from creation counts the waiting as working. coalesce keeps it
                # at the FIRST step advance — a later one must not reset the clock.
                "  started_at = coalesce(started_at, now()), "
                # A COMPLETED STEP RESETS THE RETRY BUDGET, and that is what turns `retry_count` from a
                # restart counter into a failure counter. Nothing in the tree ever reset it: recovery
                # incremented it on every daemon start and at 3 the task was marked failed outright with
                # "exhausted 3 retries". Live proof from this machine: task 9df85b0b burned retries 1
                # and 2 on two restarts 3 minutes apart while it was progressing normally, and finished
                # healthy at 10:53:20 — one restart from being destroyed for no reason.
                #
                # The boot-loop ceiling this counter exists for is not weakened; it is sharpened. A task
                # that kills the daemon before ever landing a step still accumulates monotonically and
                # is stopped at 3. A task that is genuinely advancing now survives any number of
                # restarts, which is the actual requirement: work must outlive a reboot.
                "  retry_count = CASE WHEN $3 = 'done' THEN 0 ELSE retry_count END, "
                "  health_status = 'healthy', updated_at = now() "
                # Turn 8 hardening: include needs_changes / blocked_by_review so a
                # task in the rework loop can advance again once the model addresses
                # the review's findings. Terminal states still fenced out.
                "WHERE id = $2 AND status IN ('open', 'running', 'needs_changes', 'blocked_by_review')",
                task_status, task_id, status)
            await _emit_task(conn, "task.step_advanced", task_id,
                             {"seq": step_seq, "status": status,
                              "task_status": tally["task_status"], **tally},
                             publisher=self._publisher)
            # Emit progress event for observers
            await _emit_task(conn, "task.progress", task_id,
                             {"progress_type": "step_advance", "step_seq": step_seq, **tally},
                             publisher=self._publisher)
            # Emit specific step lifecycle events for observers
            if status == "done":
                await _emit_task(conn, "task.step.completed", task_id,
                                 {"seq": step_seq, "verified": verified_by is not None, **tally},
                                 publisher=self._publisher)
            elif status == "failed":
                await _emit_task(conn, "task.step.failed", task_id,
                                 {"seq": step_seq, "error": err_text[:200] if err_text else None,
                                  **tally},
                                 publisher=self._publisher)
            # Turn 7: parent auto-close cascade. When a leaf child settles (done/skipped),
            # check if its parent should now close too, and walk up the ancestor chain.
            # Cycle-guarded inside _parent_auto_close. NOT triggered on failed/running so a
            # failed child never accidentally closes its parent.
            if status in ("done", "skipped"):
                await _parent_auto_close(conn, task_id, step_seq, publisher=self._publisher)
                # Turn 7 hardening: the cascade may have just settled a parent step,
                # which could flip task_status to 'done'. The tally captured above was
                # taken BEFORE the cascade, so all_complete was False even though the
                # task now qualifies. Recompute + reissue the task-row UPDATE so the
                # inline_done pipeline actually fires.
                cascaded_tally = await _compute_tally(conn, task_id)
                if (cascaded_tally["task_status"] == "done"
                        and tally["task_status"] != "done"):
                    tally = cascaded_tally
                    all_complete = True
                    inline_done = all_complete and self._reviewer is None
                    task_status = "done" if inline_done else "running"
                    await conn.execute(
                        "UPDATE task SET status = $1, "
                        "  is_primary = CASE WHEN $1 = 'done' THEN false ELSE is_primary END, "
                        "  last_progress_at = now(), last_progress_type = 'step_advance', "
                        "  started_at = coalesce(started_at, now()), "
                        "  retry_count = CASE WHEN $1 = 'done' THEN 0 ELSE retry_count END, "
                        "  health_status = 'healthy', updated_at = now() "
                        # Turn 8 hardening: parity with the main advance() UPDATE above.
                        "WHERE id = $2 AND status IN ('open', 'running', 'needs_changes', 'blocked_by_review')",
                        task_status, task_id)
        # The arc moves with the actual work: the first completed step is where Sali stops planning and
        # starts practising. Idempotent — advance() only matches a row that has not settled.
        if status == "done":
            with contextlib.suppress(Exception):
                from sali.learning.capability_acquisition import CapabilityAcquisitionStore

                acq = CapabilityAcquisitionStore(self.pool, self._publisher)
                live = await acq.for_task(task_id)
                if live is not None and live["status"] == "gap_identified":
                    await acq.advance(UUID(str(live["id"])), "acquiring",
                                      notes=f"step {step_seq} completed")
        # Turn 3: bare-store auto-complete runs the FULL extraction+cleanup path. Ordering pinned
        # by the adversarial review: extract FIRST (so Almir's "Finished" chat reflects a real
        # lesson attempt), then announce, then preserve+archive+cleanup. _verify_learning_candidates
        # is SKIPPED here - it promotes candidates to reviewer-verified confidence and only a real
        # reviewer PASS gives that authority; bare-store has no reviewer.
        if inline_done:
            # Turn 7: emit task.finished FIRST, before announce/extract/archive. Bare-store
            # completions previously only sent an agent.message so observers keying off
            # task.finished silently missed them. Standardize `via` so subscribers can
            # distinguish auto-complete (this branch) from review-gated / explicit finish().
            # Turn 7 hardening: suppress-guarded (was: unguarded pool.acquire could break the
            # whole completion pipeline on a transient pool hiccup); publisher.emit doesn't
            # need a conn, so pass None and let _emit_task take the publisher path.
            with contextlib.suppress(Exception):
                await _emit_task(None, "task.finished", task_id,
                                 {"status": "done", "via": "auto"},
                                 publisher=self._publisher)
            extracted = False
            with contextlib.suppress(Exception):
                extracted = await self._extract_experience(task_id)
            await self.announce_completion(task_id, "done")
            if extracted:
                with contextlib.suppress(Exception):
                    await self._preserve_artifacts(task_id)
            with contextlib.suppress(Exception):
                await self._archive_and_cleanup(task_id)
            if extracted:
                with contextlib.suppress(Exception):
                    await self._cleanup_workspace(task_id)
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
            # Turn 3 (adversarial-review-hardened) ordering: extract FIRST, then announce, then
            # preserve, then archive (metadata), then cleanup (deletes workspace). This way the
            # archive's own artifact-copy step still sees the files (workspace not deleted yet)
            # and Almir's "Finished" chat only fires after the lesson landed.
            extracted = await self._extract_experience(task_id)
            await self.announce_completion(task_id, "done")
            await self._verify_learning_candidates(task_id)  # evidence-backed by the reviewer PASS (§19)
            if extracted:
                with contextlib.suppress(Exception):
                    await self._preserve_artifacts(task_id)
            with contextlib.suppress(Exception):
                await self._archive_and_cleanup(task_id)
            if extracted:
                with contextlib.suppress(Exception):
                    await self._cleanup_workspace(task_id)   # EPHEMERAL workspace only, durably (§24-26)
            return (None, None)
        # Return None if archived (task deleted from DB), otherwise return the task.
        return (await self.get(task_id), None)

    async def announce_completion(self, task_id: UUID, status: str, result: str | None = None) -> None:
        """Tell Almir, in his CHAT, that a piece of background work ended.

        This must be called from EVERY path that completes a task. `finish()` is not the only one:
        `advance()` auto-archives when the last step is marked (both the bare-store branch and the
        reviewer-gated branch) and returns straight to the caller. A task therefore finished, archived
        and DELETED its own rows while Almir was told nothing anywhere — he watched a task disappear and
        had to ask what happened. Read the objective BEFORE the archive deletes it.
        """
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            from sali.runtime.session import persistent_session_id

            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT objective, result FROM task WHERE id = $1", task_id)
            objective = (row["objective"] if row else "") or "the task"
            body = result or (row["result"] if row else None)
            if status == "done":
                text = f"Finished: {objective}."
                importance = "completion"
            else:
                text = f"Stopped work on: {objective} ({status})."
                importance = "failure" if status == "failed" else "update"
            if body:
                text += f"\n\n{str(body)[:600]}"
            await self._publisher.emit(
                event_type="agent.message", session_id=persistent_session_id(),
                task_id=task_id, origin="agent",
                data={"text": text[:2000], "importance": importance, "channel": "agent_message"})

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
                # Every terminal state, not only success: "how long did it take to fail" is as real a
                # question as how long it took to finish, and a cancelled task still ended at a time.
                "  completed_at = coalesce(completed_at, now()), "
                "  is_primary = false, updated_at = now() "
                "WHERE id = $3", status, result, task_id)
            await _emit_task(conn, "task.finished", task_id, {"status": status}, publisher=self._publisher)
            # Turn 3 (hardening): announce moves AFTER the extract-then-preserve pipeline below
            # for status=="done", so Almir's chat message reflects a real lesson attempt. For
            # failed/abandoned, announce still fires here (extract is skipped in those paths).
            if status != "done":
                await self.announce_completion(task_id, status, result)

        # NEGATIVE EVIDENCE. The experience hook only fires on a PASS, so a failed task taught the
        # capability layer nothing — confidence could only ever go up. If Sali had a capability that was
        # supposed to carry this job and the job failed, that is exactly the signal that should weaken it.
        # Conservative on purpose: only the single best keyword match, only an unverified unsuccessful
        # use (which cannot promote status and does not touch last_verified), so a mis-attribution costs
        # one row in the usage log rather than a wrongly retired skill.
        if status in ("failed", "abandoned"):
            with contextlib.suppress(Exception):
                from sali.learning.capability import CapabilityStore

                async with self.pool.acquire() as conn:
                    obj = await conn.fetchval("SELECT objective FROM task WHERE id = $1", task_id)
                if obj:
                    caps = CapabilityStore(self.pool, self._publisher)
                    found = await caps.discover_for_task(obj, limit=1)
                    if found:
                        await caps.record_use(
                            name=found[0]["name"], scope=found[0].get("scope") or "environment",
                            scope_ref=found[0].get("scope_ref"),
                            action=obj[:200], result=(result or f"task {status}")[:400],
                            success=False, verified=False, task_id=task_id)
            await self._close_capability_gap(task_id, result or f"task {status}")

        extracted = False
        if status == "done":  # reviewer PASSED → its learning candidates are now evidence-backed (§19)
            extracted = await self._extract_experience(task_id)
            # Announce AFTER extract so the "Finished" message reflects a real lesson attempt.
            await self.announce_completion(task_id, status, result)
            await self._verify_learning_candidates(task_id)
            # The experience hook settles the acquisition when it yields a capability. If it didn't —
            # no procedure, nothing reusable — the gap is still open, and this is the last moment the
            # task id still exists to find it by (the archive below nulls the link).
            await self._close_capability_gap(task_id, "task completed without a reusable capability")
            if extracted:
                # Turn 3: preserve → archive → cleanup ordering so archive's own file-copy step
                # (in _archive_and_cleanup) still sees the workspace intact and sets
                # archived_name/filename/size on artifact records. Cleanup runs last.
                # PRESERVE THE DELIVERABLES BEFORE THE WORKSPACE THAT HOLDS THEM IS DELETED.
                #
                # The archive step below copies each artifact file out before its DB row cascades
                # away — but it runs after this cleanup, so on an ephemeral workspace the files were
                # already gone when the copy ran (`src.is_file()` simply came back False), the rows
                # were deleted, and the deliverable Sali had just announced existed nowhere. Almir hit
                # exactly this: "there is no such file", about a document Sali reported as written.
                #
                # The order cannot simply be swapped — `cleanup_task` reads the task row to find the
                # workspace, and the archive deletes that row ("Reads the task BEFORE the caller
                # archives/deletes it"). So the FILES are preserved here and the ROWS are archived
                # afterwards, each before the thing that destroys it.
                await self._preserve_artifacts(task_id)
        # Turn 3: archive is metadata-only under Turn 1 (row stays with archived_at set), so always
        # fire it - the row must land as archived on every completion path regardless of whether an
        # experience hook was wired. Runs BEFORE cleanup so archive's own file-copy step still sees
        # the workspace intact.
        with contextlib.suppress(Exception):
            await self._archive_and_cleanup(task_id)
        if extracted and status == "done":
            with contextlib.suppress(Exception):
                await self._cleanup_workspace(task_id)  # EPHEMERAL workspace only, durably (§24-26)
        return None

    async def _preserve_artifacts(self, task_id: UUID) -> None:
        """Copy this task's artifact files into its archive directory, before anything deletes them.

        Idempotent and shared with `_archive_and_cleanup`, which copies again for the rows it snapshots
        — the second copy is a no-op because the destination already exists."""
        with contextlib.suppress(Exception):
            import shutil as _sh
            from pathlib import Path as _Path

            from sali.tasks.logger import _task_dir

            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, artifact_path FROM task_artifact WHERE task_id = $1", task_id)
            adir = _task_dir(task_id) / "artifacts"
            for row in rows:
                src = _Path(row["artifact_path"])
                if not src.is_file():
                    continue
                adir.mkdir(parents=True, exist_ok=True)
                dest = adir / f"{row['id']}_{src.name}"
                if not dest.exists():
                    _sh.copy2(src, dest)

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
                "SELECT id, artifact_path, artifact_type, tool_name, created_at "
                "FROM task_artifact WHERE task_id = $1 ORDER BY created_at", task_id)
            reviews = await conn.fetch(
                "SELECT review_id, run_id, attempt, status, reviewer_type, summary, "
                "  requirements_checked, failures, recommendations, passed_count, failed_count, "
                "  unknown_count, started_at, completed_at "
                "FROM task_review WHERE task_id = $1 ORDER BY attempt", task_id)

        # Durable artifact copies (audit): the DB rows CASCADE away below and an ephemeral workspace
        # may be cleaned, which made every artifact unreachable the moment a task completed. Copy the
        # files into the archive so downloads keep working after completion.
        arts = [dict(a) for a in artifacts]
        with contextlib.suppress(Exception):
            import shutil as _sh
            from pathlib import Path as _Path

            from sali.tasks.logger import _task_dir
            adir = _task_dir(task_id) / "artifacts"
            for a in arts:
                src = _Path(a["artifact_path"])
                if src.is_file():
                    adir.mkdir(parents=True, exist_ok=True)
                    dest = adir / f"{a['id']}_{src.name}"
                    if not dest.exists():
                        _sh.copy2(src, dest)
                    a["archived_name"] = dest.name
                    a["filename"] = src.name
                    a["size"] = dest.stat().st_size

        # Archive to filesystem FIRST — if this fails, task stays in DB (safe).
        save_task_record(
            task_id, dict(task), [dict(s) for s in steps],
            executions=[dict(e) for e in executions],
            artifacts=arts,
            reviews=[dict(r) for r in reviews])
        append_event(task_id, "task_archived",
                     {"status": task["status"], "objective": task["objective"]})

        # ARCHIVE-BY-STAMPING. The row STAYS after snapshot - it is now the durable record
        # for §15 follow-up detection, §13 review history, and iOS history browsing. The
        # filesystem JSON snapshot above remains as a grep-able export; both persist.
        # (Was: DELETE FROM task WHERE id = $1 - which is what emptied the tables.)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sali_state SET active_task_id = NULL WHERE active_task_id = $1", task_id)
            await conn.execute(
                "UPDATE sali_state SET previous_task_id = NULL WHERE previous_task_id = $1", task_id)
            await conn.execute(
                "UPDATE task SET archived_at = now(), is_primary = false WHERE id = $1", task_id)

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
        # The new task carries the objective forward and opens its own gap; this one has nothing left
        # driving it.
        await self._close_capability_gap(old_task_id, f"superseded by task {new_task_id}")

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
        await self._close_capability_gap(task_id, f"task cancelled: {reason}" if reason else "task cancelled")

    async def resume(self, task_id: UUID) -> Task | None:
        """Resume a superseded/paused/cancelled task. It becomes the new primary active task.

        Accepts tasks with superseded_by IS NOT NULL or status in ('paused','cancelled'),
        because supersede() preserves execution status — a superseded task might be 'running'.

        Turn 2: refuses any task that has an active revoked_intent tombstone. Every resume
        caller (runtime.resume_primary, /tasks/{id}/resume, TaskAuthority.RESUME branch)
        funnels through here, so this one check closes every "revoke then resume revives it"
        path in one place.
        """
        from sali.tasks.revocation import RevocationStore
        if await RevocationStore(self.pool).is_revoked(task_id):
            return None
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE task SET is_primary = false WHERE is_primary")
            await conn.execute(
                "UPDATE task SET is_primary = true, status = 'running', "
                "  superseded_by = NULL, updated_at = now() "
                "WHERE id = $1 AND (superseded_by IS NOT NULL OR status IN ('paused','cancelled'))",
                task_id)
            await _emit_task(conn, "task.resumed", task_id, {}, publisher=self._publisher)
            task = await conn.fetchrow(
                "SELECT * FROM task WHERE id = $1 AND archived_at IS NULL", task_id)
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
            row = await conn.fetchrow(
                "INSERT INTO task_artifact (task_id, artifact_path, artifact_type, tool_name) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                task_id, artifact_path, artifact_type, tool_name)
        # Durable "Sali produced a file" event (audit): carries the metadata + download URL so chat
        # clients can render the file card the moment it exists — replayable like every other event.
        if self._publisher is not None and row is not None:
            from pathlib import Path as _Path
            with contextlib.suppress(Exception):
                p = _Path(artifact_path)
                try:
                    size = p.stat().st_size if p.is_file() else None
                except OSError:
                    size = None
                await self._publisher.emit(
                    event_type="task.artifact.created", task_id=task_id,
                    subject_type="artifact", subject_id=row["id"], origin="task",
                    data={"artifact_id": str(row["id"]), "filename": p.name,
                          "artifact_type": artifact_type, "tool_name": tool_name, "size": size,
                          "download_url":
                              f"/api/v1/tasks/{task_id}/artifacts/{row['id']}/download"})

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

    async def live_activity(self, task_id: UUID) -> dict[str, Any]:
        """What Sali is doing on this task RIGHT NOW, for the Tasks screen.

        Almir's complaint, verbatim: "in the app tasks are not realtime i can't see what sali doing or
        where he's now". Everything needed was already being recorded — `task_execution` rows carry the
        tool, its status and a one-line result; `agent_runs` says whether a turn is in flight — and none
        of it reached the API, so a working task looked identical to a stalled one: "1 step done" and
        silence. He read that as finished. It was on iteration 9 at the time.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tool_name, status, step_seq, started_at, "
                "       coalesce(result_summary, error) AS detail "
                "FROM task_execution WHERE task_id = $1 ORDER BY started_at DESC LIMIT 1", task_id)
            # A turn genuinely in flight is the difference between "working" and "stopped".
            working = await conn.fetchval(
                "SELECT true FROM agent_runs WHERE status = 'running' "
                "  AND user_input LIKE 'TASK:%' LIMIT 1")
        return {
            "working": bool(working),
            "current_step": await self.current_step(task_id),
            "last_tool": row["tool_name"] if row else None,
            "last_tool_status": row["status"] if row else None,
            "last_detail": (row["detail"] or "")[:160] if row else None,
            "last_activity_at": row["started_at"] if row else None,
        }

    async def current_step(self, task_id: UUID) -> int | None:
        """The seq of the step currently being worked on (status='running'), or None.

        Used by the loop to record executions against the correct step instead of hardcoding 0.
        Returns the first running step (by seq order) since only one step runs at a time.
        """
        async with self.pool.acquire() as conn:
            # NOTHING EVER SETS A STEP 'running'. `advance_task` moves a step straight from pending to
            # done, so this only ever returned None and every tool execution was filed against step 0 —
            # measured on a live task: web_search, web_fetch, list_directory, create_file and
            # advance_task ALL recorded with step_seq=0 while step 1 was the one being worked. That makes
            # "where is Sali now" unanswerable, on the Tasks screen and in the experience record alike.
            # The first unfinished step is the honest answer when no step is explicitly running.
            return cast("int | None", await conn.fetchval(
                "SELECT seq FROM task_step "
                "WHERE task_id = $1 AND status = 'running' ORDER BY seq LIMIT 1",
                task_id) or await conn.fetchval(
                "SELECT seq FROM task_step "
                "WHERE task_id = $1 AND status IN ('pending','waiting','blocked') "
                "ORDER BY seq LIMIT 1", task_id))

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


async def _compute_tally(conn: Any, task_id: UUID) -> dict[str, Any]:
    """Turn 7: single source of truth for step-completion counts. Every code path that
    needs a numerator/denominator sources it here, so the auto-complete predicate, the
    events on the bus, the API response, and the iOS renderer are literally the same
    numbers.

    - done       = status='done'
    - skipped    = status='skipped'
    - verified   = status='done' AND verified_by IS NOT NULL
    - total      = row count
    - task_status = 'done' iff total > 0 AND done + skipped == total, else 'running'

    Empty task_step (total = 0) explicitly does NOT auto-complete, preserving the
    pre-Turn-7 contract (a task with no steps stays open until someone plans them).
    """
    rows = await conn.fetch(
        "SELECT status, (verified_by IS NOT NULL) AS is_verified "
        "FROM task_step WHERE task_id = $1", task_id)
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    skipped = sum(1 for r in rows if r["status"] == "skipped")
    verified = sum(1 for r in rows if r["status"] == "done" and r["is_verified"])
    task_status = "done" if total > 0 and (done + skipped) == total else "running"
    return {"done": done, "total": total, "verified": verified,
            "skipped": skipped, "task_status": task_status}


async def _parent_auto_close(
    conn: Any, task_id: UUID, child_seq: int, *, publisher: Any = None,
) -> None:
    """Turn 7: walk up the parent_seq chain, closing any parent whose children are all
    settled. A parent step is a UI-hierarchy container; today it stays open until the
    model manually calls advance() on it. Now: when the last child settles, the parent
    settles too, and the cascade walks the entire ancestor chain in one transaction.
    Cycle-guarded via a visited set (parent_seq integrity is enforced by app code, not
    the schema, so a bad row could point at itself)."""
    visited: set[int] = {child_seq}
    row = await conn.fetchrow(
        "SELECT parent_seq FROM task_step WHERE task_id = $1 AND seq = $2",
        task_id, child_seq)
    parent_seq = row["parent_seq"] if row is not None else None
    while parent_seq is not None and parent_seq not in visited:
        visited.add(parent_seq)
        # All children of THIS parent settled?
        settled = await conn.fetchval(
            "SELECT bool_and(status IN ('done','skipped')) "
            "FROM task_step WHERE task_id = $1 AND parent_seq = $2",
            task_id, parent_seq)
        if not settled:
            return  # some sibling is still unsettled — stop walking up
        # Close the parent (idempotent: only if it isn't already settled itself).
        res = await conn.execute(
            "UPDATE task_step SET status = 'done', finished_at = coalesce(finished_at, now()) "
            "WHERE task_id = $1 AND seq = $2 AND status NOT IN ('done','skipped','failed')",
            task_id, parent_seq)
        if res == "UPDATE 0":
            # Parent was already settled - walk one more level to be safe.
            row = await conn.fetchrow(
                "SELECT parent_seq FROM task_step WHERE task_id = $1 AND seq = $2",
                task_id, parent_seq)
            parent_seq = row["parent_seq"] if row is not None else None
            continue
        # Emit the same tally-bearing event for the auto-closed parent.
        tally = await _compute_tally(conn, task_id)
        await _emit_task(conn, "task.step_advanced", task_id,
                         {"seq": parent_seq, "status": "done",
                          "task_status": tally["task_status"], **tally,
                          "auto_closed": True},
                         publisher=publisher)
        # Walk one level higher.
        row = await conn.fetchrow(
            "SELECT parent_seq FROM task_step WHERE task_id = $1 AND seq = $2",
            task_id, parent_seq)
        parent_seq = row["parent_seq"] if row is not None else None


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
