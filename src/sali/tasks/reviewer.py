"""TaskReviewer — the deterministic completion gate (Prompt 4).

A task may NOT become 'done' because the model says so. The reviewer loads DURABLE evidence — step
verification, tool-execution records, and artifacts on disk — and derives a verdict from that evidence
alone: PASS / NEEDS_REWORK / BLOCKED. Only PASS permits completion. Every attempt is durable
(``task_review``), never overwritten, so the rework loop is auditable and Sali can see what it
previously failed. One logical task → many runs → many review attempts (the task_id never changes).

Deterministic and side-effect-safe: it inspects state and probes the filesystem READ-ONLY (via
``verify.engine.path_exists``); it never runs new subprocesses and never mutates task/step state. It
stores concise operational results only — evidence, never opinion; no chain-of-thought (§3/§30).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sali.core.errors import FailureClass
from sali.obs.log import get_logger
from sali.verify.engine import path_exists

log = get_logger("sali.task_reviewer")


class ReviewStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    NEEDS_REWORK = "needs_rework"
    BLOCKED = "blocked"
    FAILED = "failed"  # reserved: an internal reviewer error, not a task verdict


# Turn 3 tightening: 'attempted' (a step marked done with no tool executions) NO LONGER counts as
# passing. A pure-planning step that legitimately has nothing to verify can still be marked
# 'skipped'; but a task cannot auto-pass a review just because the model called advance(done) on
# steps that produced no evidence. Under §14 completion must be evidence-based - the vacuous-pass
# hole was exactly this frozenset. 'passed' stays for external verifiers that emit their own grade.
_PASSING = frozenset({"passed", "verified", "skipped"})


def step_evidence(status: str | None, verified: bool | None) -> str:
    """The Prompt 3 evidence grade for a step, kept in the tasks layer so the reviewer needn't import
    upward into runtime.continuation. verified / attempted / failed / pending / unknown."""
    if status == "done":
        return "verified" if verified else "attempted"
    if status == "failed":
        return "failed"
    if status in ("pending", "waiting", "blocked", "running"):
        return "pending"
    if status == "skipped":
        # Turn 3 hardening: 'skipped' is NOT evidence of verified work - it means the step was
        # explicitly not attempted. Grading it as 'verified' let a model auto-complete any task
        # by marking every step skipped, since 'verified' passes the task-level evidence check.
        return "skipped"
    return "unknown"


@dataclass(slots=True)
class ReviewResult:
    review_id: UUID
    task_id: UUID
    run_id: UUID | None
    status: ReviewStatus
    attempt: int
    reviewer_type: str
    summary: str
    requirements: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    unknown: int = 0

    @property
    def is_pass(self) -> bool:
        return self.status is ReviewStatus.PASSED

    def to_public(self) -> dict[str, Any]:
        return {
            "review_id": str(self.review_id), "task_id": str(self.task_id),
            "run_id": str(self.run_id) if self.run_id else None,
            "status": self.status.value, "attempt": self.attempt,
            "reviewer_type": self.reviewer_type, "summary": self.summary,
            "requirements": self.requirements, "failures": self.failures,
            "recommendations": self.recommendations,
            "passed": self.passed, "failed": self.failed, "unknown": self.unknown,
        }


def _row_to_result(row: Any) -> ReviewResult:
    return ReviewResult(
        review_id=row["review_id"], task_id=row["task_id"], run_id=row["run_id"],
        status=ReviewStatus(row["status"]), attempt=row["attempt"],
        reviewer_type=row["reviewer_type"], summary=row["summary"] or "",
        requirements=list(row["requirements_checked"] or []),
        failures=list(row["failures"] or []),
        recommendations=list(row["recommendations"] or []),
        passed=row["passed_count"], failed=row["failed_count"], unknown=row["unknown_count"])


class TaskReviewer:
    """Runs the deterministic completion review and persists every attempt. Injected into the TaskStore
    (the completion choke point) and exposed to tools via the ToolContext, the same inversion the
    publisher uses — so the LLM can request a review but can never BE the reviewer."""

    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def latest_review(self, task_id: UUID) -> ReviewResult | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM task_review WHERE task_id = $1 ORDER BY attempt DESC LIMIT 1", task_id)
        return _row_to_result(row) if row is not None else None

    async def latest_terminal_review(self, task_id: UUID) -> ReviewResult | None:
        """The latest SETTLED verdict — excludes a 'running' row left dangling by a crash mid-review, so
        the continuation block reflects the last real verdict (a subsequent PASS still wins), never a
        half-written attempt. Correctness-safe: the completion gate always runs a FRESH review anyway."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM task_review WHERE task_id = $1 AND status <> 'running' "
                "ORDER BY attempt DESC LIMIT 1", task_id)
        return _row_to_result(row) if row is not None else None

    async def reviews(self, task_id: UUID) -> list[dict[str, Any]]:
        """Full review history (oldest first) — durable attempts, for the API/observability (§14/§24)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_review WHERE task_id = $1 ORDER BY attempt", task_id)
        return [_row_to_result(r).to_public() for r in rows]

    async def has_passing_review(self, task_id: UUID) -> bool:
        latest = await self.latest_review(task_id)
        return latest is not None and latest.status is ReviewStatus.PASSED

    async def review(
        self, task_id: UUID, *, run_id: UUID | None = None, reviewer_type: str = "deterministic",
    ) -> ReviewResult:
        """Inspect durable evidence and return a verdict. Persists a durable attempt and emits events.
        Never mutates task/step state — the caller (TaskStore) decides completion from the verdict."""
        review_id = uuid4()
        async with self._pool.acquire() as conn:
            task = await conn.fetchrow("SELECT id, objective, status FROM task WHERE id = $1", task_id)
            if task is None:
                return ReviewResult(review_id, task_id, run_id, ReviewStatus.BLOCKED, 0,
                                    reviewer_type, "task not found")
            steps = await conn.fetch(
                "SELECT seq, description, status, verified, failure_class, last_error, attempts "
                "FROM task_step WHERE task_id = $1 ORDER BY seq", task_id)
            execs = await conn.fetch(
                "SELECT step_seq, tool_name, status, error FROM task_execution WHERE task_id = $1", task_id)
            arts = await conn.fetch(
                "SELECT artifact_path, artifact_type FROM task_artifact WHERE task_id = $1 "
                "ORDER BY created_at", task_id)
            attempt = int(await conn.fetchval(
                "SELECT coalesce(max(attempt), 0) + 1 FROM task_review WHERE task_id = $1", task_id))
            await conn.execute(
                "INSERT INTO task_review (review_id, task_id, run_id, attempt, status, reviewer_type) "
                "VALUES ($1, $2, $3, $4, 'running', $5)", review_id, task_id, run_id, attempt, reviewer_type)
        await self._emit("review.started", task_id, run_id, review_id,
                         {"attempt": attempt, "steps": len(steps), "artifacts": len(arts)})

        exec_by_step: dict[int, list[Any]] = {}
        for e in execs:
            exec_by_step.setdefault(e["step_seq"], []).append(e)

        requirements: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        recommendations: list[dict[str, Any]] = []
        blocked = False

        for s in steps:
            grade = step_evidence(s["status"], s["verified"])
            step_execs = exec_by_step.get(s["seq"], [])
            failed_execs = [e for e in step_execs if e["status"] == "failed"]
            ok_execs = [e for e in step_execs if e["status"] in ("completed", "verified_success")]
            desc = f"step {s['seq']}: {s['description']}"
            req: dict[str, Any] = {"requirement": desc, "kind": "step", "seq": s["seq"]}
            if s["status"] == "failed":
                fc = s["failure_class"]
                err = (s["last_error"] or "")[:160]
                if fc == FailureClass.PERMISSION.value:
                    req["status"] = "blocked"
                    req["evidence"] = f"failed (permission): {err}"
                    blocked = True
                    recommendations.append({"requirement": desc,
                                            "action": "needs privilege / credential / user intervention",
                                            "evidence": req["evidence"]})
                else:
                    req["status"] = "failed"
                    req["evidence"] = f"failed ({fc or 'unknown'}): {err}"
                    recommendations.append({"requirement": desc, "action": "fix and re-run this step",
                                            "evidence": req["evidence"]})
                failures.append({"requirement": desc, "evidence": req["evidence"]})
            elif grade == "pending":
                req["status"] = "pending"
                req["evidence"] = f"step is {s['status']} — work remains"
                failures.append({"requirement": desc, "evidence": req["evidence"]})
                recommendations.append({"requirement": desc, "action": "complete this step"})
            elif failed_execs and not ok_execs:
                # a 'done' step whose ONLY tool evidence is failure → evidence beats the claim (§15).
                # A failure later RECOVERED by a successful execution on the same step is the normal
                # attempt-A-fails → attempt-B-succeeds recovery path, not rework — the failure is kept as
                # negative knowledge (in the experience record), but the step legitimately passed.
                req["status"] = "failed"
                err = (failed_execs[-1]["error"] or "")[:160]
                req["evidence"] = f"a {failed_execs[-1]['tool_name']} execution failed: {err}"
                failures.append({"requirement": desc, "evidence": req["evidence"]})
                recommendations.append({"requirement": desc, "action": "re-run the failed tool"})
            else:
                req["status"] = grade  # verified | attempted | skipped
                req["evidence"] = ("verified by a tool execution" if s["verified"]
                                   else "marked done (no tool execution to verify)")
            requirements.append(req)
            await self._emit("review.requirement.checked", task_id, run_id, review_id,
                             {"requirement": desc, "status": req["status"]})

        for a in arts:
            if a["artifact_type"] not in ("created", "modified"):
                continue
            v = path_exists(a["artifact_path"])
            desc = f"artifact exists: {a['artifact_path']}"
            req = {"requirement": desc, "kind": "artifact"}
            if v is None:
                req["status"] = "unknown"
                req["evidence"] = "path not probeable (glob/relative) — not independently verified"
            elif v.success:
                req["status"] = "passed"
                req["evidence"] = v.detail
            else:
                req["status"] = "failed"
                req["evidence"] = v.detail
                failures.append({"requirement": desc, "evidence": v.detail})
                recommendations.append({"requirement": desc, "action": "recreate the missing artifact"})
            requirements.append(req)
            await self._emit("review.requirement.checked", task_id, run_id, review_id,
                             {"requirement": desc, "status": req["status"]})

        passed = sum(1 for r in requirements if r["status"] in _PASSING)
        failed = sum(1 for r in requirements if r["status"] == "failed")
        unknown = sum(1 for r in requirements if r["status"] in ("pending", "unknown", "blocked", "attempted"))

        if blocked:
            status = ReviewStatus.BLOCKED
        elif not requirements:
            # A task with NOTHING to verify has no durable evidence of completion — it must not pass
            # vacuously (Final audit §25). Reachable via a zero-step task (e.g. revive); the model must
            # plan and complete real steps before a task can be 'done'.
            status = ReviewStatus.NEEDS_REWORK
        elif failed > 0 or any(r["status"] == "pending" for r in requirements):
            status = ReviewStatus.NEEDS_REWORK
        elif not (any(r.get("kind") == "step" and r["status"] == "verified" for r in requirements)
                  or any(r.get("kind") == "artifact" and r["status"] == "passed" for r in requirements)):
            # Turn 3 (review-hardening): the task has nothing failed/pending, but ALSO no real
            # verified evidence - every step is 'attempted' (done without tool executions) or
            # 'skipped', and no artifact was independently probed. That is exactly the
            # vacuous-pass hole. A pure-planning step is legitimate as ONE step in a
            # multi-step task, but a task consisting only of pure-planning or skipped steps
            # has produced no verifiable evidence and must not auto-complete.
            status = ReviewStatus.NEEDS_REWORK
            recommendations.append({"requirement": "verified evidence",
                                    "action": "no step was verified by a tool execution and no\n                                              artifact could be probed; complete real work before\n                                              marking the task done",
                                    "evidence": ""})
        else:
            status = ReviewStatus.PASSED
        summary = (f"{passed} passed, {failed} failed, {unknown} unresolved "
                   f"of {len(requirements)} requirement(s)")

        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_review SET status = $1, summary = $2, requirements_checked = $3, "
                "  failures = $4, recommendations = $5, passed_count = $6, failed_count = $7, "
                "  unknown_count = $8, completed_at = now() WHERE review_id = $9",
                status.value, summary, requirements, failures, recommendations,
                passed, failed, unknown, review_id)

        result = ReviewResult(review_id, task_id, run_id, status, attempt, reviewer_type, summary,
                              requirements, failures, recommendations, passed, failed, unknown)
        done_event = {
            ReviewStatus.PASSED: "review.passed",
            ReviewStatus.NEEDS_REWORK: "review.needs_rework",
            ReviewStatus.BLOCKED: "review.blocked",
        }.get(status, "review.failed")
        await self._emit(done_event, task_id, run_id, review_id,
                         {"attempt": attempt, "summary": summary, "passed": passed,
                          "failed": failed, "unknown": unknown, "failures": failures[:10]})
        return result

    async def _emit(
        self, event_type: str, task_id: UUID, run_id: UUID | None, review_id: UUID,
        data: dict[str, Any],
    ) -> None:
        """Publish a reviewer event through the canonical publisher — operational state only, never
        chain-of-thought (§13/§30). Best-effort: observability must never break the gate."""
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, run_id=run_id,
                subject_type="task", subject_id=task_id, origin="reviewer",
                data={"review_id": str(review_id), **data})
