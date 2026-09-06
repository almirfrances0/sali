"""Turn 3 of the Work-audit: close the completion-gate bypass + inline auto-complete
short-circuit + drop the 'attempted' vacuous-pass grade.

Four seams pinned:
  1. Bare-store gate bypass — still permitted, but logs a warning so we see it.
  2. Inline auto-complete branch runs the FULL extraction+cleanup path (was leaking
     workspaces — the 6203-dir orphan pile).
  3. 'attempted' no longer counts as passing; a pure-planning task can't auto-pass.
  4. `_extract_experience` returns False when no hook is wired so cleanup is skipped
     (nothing was learned; do not destroy the evidence).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sali.tasks.reviewer import _PASSING, TaskReviewer
from sali.tasks.store import TaskStore


# ── FIX 3: 'attempted' no longer passes ─────────────────────────────────────────

def test_attempted_removed_from_passing_set() -> None:
    """The vacuous-pass grade is out; only real evidence counts."""
    assert "attempted" not in _PASSING
    assert _PASSING == frozenset({"passed", "verified", "skipped"})


@pytest.mark.asyncio
async def test_pure_planning_task_does_not_auto_pass_review(live_pool) -> None:
    """A task whose only step is marked 'done' with no tool executions must NOT pass a
    fresh review under the tightened _PASSING. Under the old contract it slipped through."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    store = TaskStore(live_pool)
    task = await store.create("Plan the migration", ["design the layout"])
    # Mark the single step done with NO tool_execution behind it. Under the old contract
    # this graded 'attempted' → passing; under the new one it grades 'attempted' → NOT
    # in _PASSING → reviewer returns NEEDS_REWORK.
    await store.advance(task.id, 1, "done")

    reviewer = TaskReviewer(live_pool, publisher=None)
    verdict = await reviewer.review(task.id)
    assert not verdict.is_pass, (
        f"pure-planning task must not auto-pass; got status={verdict.status}")


# ── FIX 5: hook is None → return False so workspace is preserved ───────────────

@pytest.mark.asyncio
async def test_extract_experience_returns_false_when_no_hook() -> None:
    """Bare store has no lesson to extract; must return False so callers preserve
    the workspace (nothing was learned; cleanup would destroy the only evidence)."""
    class _P:
        async def acquire(self):
            raise AssertionError("no DB call expected")
    store = TaskStore(_P())
    assert store._experience_hook is None
    result = await store._extract_experience(uuid4())
    assert result is False


# ── FIX 1: bare-store gate emits a warning log ─────────────────────────────────

@pytest.mark.asyncio
async def test_bare_store_gate_bypass_emits_warning(live_pool, caplog) -> None:
    """The bare-store gate still lets tests through, but production paths that
    accidentally hit it emit a WARNING to sali.task_store so we notice."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    store = TaskStore(live_pool)      # no reviewer wired
    task_id = uuid4()
    caplog.set_level(logging.WARNING, logger="sali.task_store")
    result = await store._review_gate(task_id, run_id=None)
    assert result is True, "bare-store gate must still permit (Turn 3 is warn-not-fail)"
    warnings = [r for r in caplog.records
                if r.name == "sali.task_store" and r.levelno == logging.WARNING]
    assert warnings, (
        "bare-store gate bypass MUST emit a warning so we can catch any production path "
        "that constructs TaskStore without wiring a reviewer")
    assert any("bare_task_store_gate_bypassed" in r.getMessage() for r in warnings)


# ── FIX 2: inline auto-complete extracts + cleans workspace ────────────────────

@pytest.mark.asyncio
async def test_inline_autocomplete_runs_experience_extraction_hook(live_pool) -> None:
    """The bare-store advance() auto-complete branch used to skip _extract_experience
    entirely - a lesson was never even attempted. Now the hook fires as part of the
    inline path, mirroring the reviewer-gated branch."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    hook_calls: list[UUID] = []

    async def _hook(task_id: UUID) -> None:
        hook_calls.append(task_id)

    store = TaskStore(live_pool)
    store._experience_hook = _hook

    task = await store.create("Ship the thing", ["step one"])
    # An execution behind the step so evidence gate passes.
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 1, "done", verified_by=exec_id)

    assert hook_calls == [task.id], (
        "inline auto-complete must fire the experience hook; the old branch skipped it "
        "and 6203 tasks completed with no lesson written")


@pytest.mark.asyncio
async def test_inline_autocomplete_invokes_cleanup_and_archive(live_pool) -> None:
    """The inline auto-complete branch used to fire announce + archive but SKIP
    experience-extraction, preserve-artifacts, and cleanup-workspace. That's what
    leaked 6203 sali-works dirs. Spy on the three methods to prove they now fire
    in the right order: extract → preserve → cleanup → archive."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    calls: list[str] = []

    async def _hook(task_id: UUID) -> None:
        calls.append("extract")

    store = TaskStore(live_pool)
    store._experience_hook = _hook

    # Spy on the three cleanup-side methods without changing what they do to the DB.
    _orig_preserve = store._preserve_artifacts
    _orig_cleanup = store._cleanup_workspace
    _orig_archive = store._archive_and_cleanup

    async def spy_preserve(tid: UUID) -> None:
        calls.append("preserve"); await _orig_preserve(tid)
    async def spy_cleanup(tid: UUID) -> None:
        calls.append("cleanup"); await _orig_cleanup(tid)
    async def spy_archive(tid: UUID) -> None:
        calls.append("archive"); await _orig_archive(tid)
    store._preserve_artifacts = spy_preserve   # type: ignore[assignment]
    store._cleanup_workspace = spy_cleanup     # type: ignore[assignment]
    store._archive_and_cleanup = spy_archive   # type: ignore[assignment]

    task = await store.create("Task with a real step", ["only step"])
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 1, "done", verified_by=exec_id)

    # Every cleanup-side method must fire — the old inline branch skipped the first three.
    assert "extract" in calls, "experience-extraction hook must fire on inline auto-complete"
    assert "preserve" in calls, "artifact preservation must fire on inline auto-complete"
    assert "cleanup"  in calls, "workspace cleanup must fire on inline auto-complete"
    assert "archive"  in calls, "archive-and-cleanup must fire on inline auto-complete"
    # Turn 3 (hardened) ordering: extract → preserve → archive → cleanup, so archive's own
    # file-copy step still sees the workspace intact when it stamps artifact metadata.
    assert calls.index("extract") < calls.index("preserve")
    assert calls.index("preserve") < calls.index("archive")
    assert calls.index("archive") < calls.index("cleanup")

    # Turn 1 preserved-row invariant still holds — the row survives with archived_at set.
    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT status, archived_at FROM sali.task WHERE id=$1", task.id)
    assert row is not None and row["status"] == "done" and row["archived_at"] is not None


@pytest.mark.asyncio
async def test_bare_store_completion_skips_preserve_and_cleanup_when_no_hook(
    live_pool,
) -> None:
    """Fix 5 corollary: with no experience hook wired, `_extract_experience` returns
    False, so preserve+cleanup are skipped (nothing was learned; do not destroy the
    evidence). Only the archive-and-cleanup metadata step still fires so the row
    lands as archived per Turn 1."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    calls: list[str] = []
    store = TaskStore(live_pool)   # no reviewer, no hook
    _orig_preserve = store._preserve_artifacts
    _orig_cleanup = store._cleanup_workspace
    _orig_archive = store._archive_and_cleanup

    async def spy_preserve(tid: UUID) -> None:
        calls.append("preserve"); await _orig_preserve(tid)
    async def spy_cleanup(tid: UUID) -> None:
        calls.append("cleanup"); await _orig_cleanup(tid)
    async def spy_archive(tid: UUID) -> None:
        calls.append("archive"); await _orig_archive(tid)
    store._preserve_artifacts = spy_preserve   # type: ignore[assignment]
    store._cleanup_workspace = spy_cleanup     # type: ignore[assignment]
    store._archive_and_cleanup = spy_archive   # type: ignore[assignment]

    task = await store.create("bare-store task", ["step"])
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 1, "done", verified_by=exec_id)

    # Fix 5: preserve + cleanup MUST NOT fire (no lesson to extract → no permission to
    # destroy the workspace). Archive-metadata still lands so the row is preserved
    # with archived_at set.
    assert "preserve" not in calls
    assert "cleanup"  not in calls
    assert "archive"  in calls, "archive must still fire so the row is metadata-preserved"


# ── Adversarial-review hardening tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_skipped_steps_do_NOT_auto_pass(live_pool) -> None:
    """Finding #1: without a task-level check, a model can mark every step 'skipped'
    (step_evidence returns 'verified' for skipped, all in _PASSING) and auto-complete
    with zero tool executions. The task-level "at least one verified step OR passing
    artifact" gate must block this."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    from sali.events.publisher import EventPublisher
    from sali.tasks.reviewer import ReviewStatus

    pub = EventPublisher(live_pool)
    reviewer = TaskReviewer(live_pool, pub)
    store = TaskStore(live_pool, pub, reviewer)

    task = await store.create("Do the thing", ["step 1", "step 2", "step 3"])
    await store.activate(task.id)
    # Mark ALL steps skipped (the exploit)
    await store.advance(task.id, 1, "skipped")
    await store.advance(task.id, 2, "skipped")
    await store.advance(task.id, 3, "skipped", run_id=uuid4())

    # Reviewer must return NEEDS_REWORK (no real evidence)
    verdict = await reviewer.review(task.id)
    assert verdict.status is ReviewStatus.NEEDS_REWORK, (
        f"all-skipped task must not pass; got {verdict.status}. "
        f"summary: {verdict.summary}")


@pytest.mark.asyncio
async def test_pure_planning_step_in_multi_step_task_still_passes(live_pool) -> None:
    """Finding #2 (my regression): a task with ONE pure-planning step + ONE verified
    step must still pass. The task-level check is "at least one verified step OR
    passing artifact" — not "every step must be verified"."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    from sali.events.publisher import EventPublisher
    from sali.tasks.reviewer import ReviewStatus

    pub = EventPublisher(live_pool)
    reviewer = TaskReviewer(live_pool, pub)
    store = TaskStore(live_pool, pub, reviewer)

    task = await store.create("plan then do", ["design the layout", "ship the code"])
    await store.activate(task.id)
    # Step 1: pure planning, no execution, marked done (grade='attempted')
    await store.advance(task.id, 1, "done")
    # Step 2: has a tool execution, marked done with verified_by (grade='verified')
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 2, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 2, "done", verified_by=exec_id, run_id=uuid4())

    verdict = await reviewer.review(task.id)
    assert verdict.status is ReviewStatus.PASSED, (
        f"task with one pure-planning step + one verified step must pass; "
        f"got {verdict.status}. summary: {verdict.summary}")


@pytest.mark.asyncio
async def test_announce_completion_fires_AFTER_extract_experience(live_pool) -> None:
    """Finding #3: Almir must not be told 'Finished' before the lesson is extracted.
    Reordering means announce fires AFTER extract across every completion path."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    call_order: list[str] = []

    async def _hook(task_id: UUID) -> None:
        call_order.append("extract")

    store = TaskStore(live_pool)
    store._experience_hook = _hook

    _orig_announce = store.announce_completion

    async def spy_announce(tid: UUID, status: str, result=None) -> None:
        call_order.append("announce")
        await _orig_announce(tid, status, result)
    store.announce_completion = spy_announce   # type: ignore[assignment]

    task = await store.create("Do stuff", ["one"])
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 1, "done", verified_by=exec_id)

    assert "extract" in call_order and "announce" in call_order
    assert call_order.index("extract") < call_order.index("announce"), (
        f"extract MUST fire before announce; got order {call_order}. "
        f"Almir's 'Finished' message must reflect a real lesson attempt.")


@pytest.mark.asyncio
async def test_bare_store_inline_done_does_NOT_verify_learning_candidates(live_pool) -> None:
    """Finding #4: bare-store has no reviewer that ran, so _verify_learning_candidates
    (which promotes to evidence_level>=5 = "reviewer-verified") MUST NOT fire in the
    inline_done branch."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    called = False

    async def _hook(task_id: UUID) -> None:
        pass

    store = TaskStore(live_pool)
    store._experience_hook = _hook

    _orig_verify = store._verify_learning_candidates

    async def spy_verify(tid: UUID) -> None:
        nonlocal called
        called = True
        await _orig_verify(tid)
    store._verify_learning_candidates = spy_verify   # type: ignore[assignment]

    task = await store.create("bare-store", ["step"])
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 1, "done", verified_by=exec_id)

    assert not called, (
        "_verify_learning_candidates promotes candidates to reviewer-verified confidence. "
        "It must not fire in the bare-store branch because no reviewer ever ran.")


@pytest.mark.asyncio
async def test_archive_runs_before_cleanup_so_artifacts_snapshot(live_pool) -> None:
    """Finding #5: _archive_and_cleanup's own file-copy step (sets archived_name /
    filename / size on artifact records) must see the workspace intact — so it MUST
    run BEFORE _cleanup_workspace, not after."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    call_order: list[str] = []

    async def _hook(task_id: UUID) -> None:
        pass

    store = TaskStore(live_pool)
    store._experience_hook = _hook

    _orig_archive = store._archive_and_cleanup
    _orig_cleanup = store._cleanup_workspace

    async def spy_archive(tid: UUID) -> None:
        call_order.append("archive")
        await _orig_archive(tid)
    async def spy_cleanup(tid: UUID) -> None:
        call_order.append("cleanup")
        await _orig_cleanup(tid)
    store._archive_and_cleanup = spy_archive   # type: ignore[assignment]
    store._cleanup_workspace = spy_cleanup     # type: ignore[assignment]

    task = await store.create("Task", ["step"])
    exec_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_execution (id, task_id, step_seq, tool_name, status, "
            "  attempt, idempotent, started_at, finished_at) "
            "VALUES ($1, $2, 1, 'write_file', 'completed', 1, TRUE, now(), now())",
            exec_id, task.id)
    await store.advance(task.id, 1, "done", verified_by=exec_id)

    assert "archive" in call_order and "cleanup" in call_order
    assert call_order.index("archive") < call_order.index("cleanup"), (
        f"archive MUST run before cleanup so its file-copy step still sees the "
        f"workspace; got order {call_order}")
