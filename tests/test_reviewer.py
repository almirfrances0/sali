"""The reviewer gate + evidence-based completion + automatic rework (Prompt 4).

The invariant under test: Sali does not decide a task is finished because he said so — a task becomes
'done' only when the deterministic reviewer proves, from durable evidence (verified steps, tool
executions, artifacts on disk), that the required work is complete. Covers the verdicts (PASS /
NEEDS_REWORK / BLOCKED), the completion gate (finish + advance can't bypass it), durable attempts,
events, idempotency, the continuation block, and a realistic rework-to-pass capstone.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.clock import SystemClock
from sali.events.publisher import EventPublisher
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime import continuation
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore
from sali.tools.builtins.task_tool import FinishTask, ReviewTask
from sali.tools.context import ToolContext
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


async def _seed(
    pool: Any, steps: list[dict[str, Any]], *,
    artifacts: tuple[tuple[str, str], ...] = (),
    executions: tuple[tuple[int, str, str, str | None], ...] = (),
    gated: bool = False,
) -> tuple[Any, TaskStore, TaskReviewer]:
    """Build a task in a precise durable state. `gated` wires the reviewer onto the store (the
    completion choke point). Returns (task, store, reviewer)."""
    pub = EventPublisher(pool)
    reviewer = TaskReviewer(pool, pub)
    store = TaskStore(pool, pub, reviewer if gated else None)
    task = await store.create("Build the app", [f"step {i}" for i in range(1, len(steps) + 1)])
    await store.activate(task.id)
    async with pool.acquire() as c:
        for i, s in enumerate(steps, 1):
            await c.execute(
                "UPDATE task_step SET status = $1, verified = $2, "
                "  failure_class = $3::task_failure_class, last_error = $4 "
                "WHERE task_id = $5 AND seq = $6",
                s["status"], s.get("verified", False), s.get("failure_class"),
                s.get("last_error"), task.id, i)
    for path, atype in artifacts:
        await store.record_artifact(task.id, path, atype)
    for seq, tool, st, err in executions:
        exid = await store.record_execution(task.id, seq, tool)
        await store.complete_execution(exid, status=st, error=err)
    return task, store, reviewer


async def _events(pool: Any, task_id: Any, prefix: str = "review.") -> list[dict[str, Any]]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT event_type, payload FROM event WHERE subject_id = $1 AND event_type LIKE $2 "
            "ORDER BY seq", task_id, f"{prefix}%")
    return [{"type": r["event_type"], "payload": dict(r["payload"])} for r in rows]


# ── verdicts: the reviewer reads evidence, not the model's word (§5/§6/§7) ──────────────────────────

async def test_all_steps_verified_passes(live_pool: Any) -> None:
    task, _store, reviewer = await _seed(live_pool, [
        {"status": "done", "verified": True}, {"status": "done", "verified": True}])
    r = await reviewer.review(task.id, run_id=uuid4())
    assert r.status is ReviewStatus.PASSED and r.failed == 0 and r.is_pass  # (§22.2/9)


async def test_incomplete_step_needs_rework(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [
        {"status": "done", "verified": True}, {"status": "pending"}])
    r = await reviewer.review(task.id)
    assert r.status is ReviewStatus.NEEDS_REWORK  # a pending step is not complete (§22.5)
    assert any("pending" in f["evidence"] for f in r.failures)


async def test_unverified_done_step_with_failed_execution_needs_rework(live_pool: Any) -> None:
    # A step marked 'done' but contradicted by a FAILED tool execution → evidence beats the claim.
    task, _s, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        executions=((1, "run_tests", "failed", "2 assertions failed"),))
    r = await reviewer.review(task.id)
    assert r.status is ReviewStatus.NEEDS_REWORK  # (§22.6/8)
    assert any("run_tests" in f["evidence"] for f in r.failures)


async def test_successful_test_execution_passes(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        executions=((1, "run_tests", "completed", None),))
    r = await reviewer.review(task.id)
    assert r.status is ReviewStatus.PASSED  # a passing test is passing evidence (§22.9)


async def test_missing_artifact_fails_review(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        artifacts=(("/definitely/not/here/index.html", "created"),))
    r = await reviewer.review(task.id)
    assert r.status is ReviewStatus.NEEDS_REWORK  # (§22.7)
    assert any("index.html" in f["requirement"] for f in r.failures)


async def test_existing_artifact_passes_review(live_pool: Any) -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "index.html")
        Path(p).write_text("<html></html>")
        task, _s, reviewer = await _seed(
            live_pool, [{"status": "done", "verified": True}], artifacts=((p, "created"),))
        r = await reviewer.review(task.id)
        assert r.status is ReviewStatus.PASSED  # the artifact really exists on disk (§22.28)


async def test_permission_failure_is_blocked(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [
        {"status": "done", "verified": True},
        {"status": "failed", "failure_class": "permission", "last_error": "sudo: a password is required"}])
    r = await reviewer.review(task.id)
    assert r.status is ReviewStatus.BLOCKED  # needs human/credential — not auto-reworkable (§22.4/§15)


async def test_transient_failure_needs_rework_not_blocked(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [
        {"status": "failed", "failure_class": "transient", "last_error": "connection reset"}])
    r = await reviewer.review(task.id)
    assert r.status is ReviewStatus.NEEDS_REWORK  # actionable — fix and retry (§22.25)


# ── the completion gate: finish/advance can't bypass the reviewer (§8) ──────────────────────────────

async def test_finish_done_blocked_without_passing_review(live_pool: Any) -> None:
    # all steps done+verified, but a recorded artifact is missing → the review gate rejects completion.
    task, store, _r = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        artifacts=(("/nope/missing.bin", "created"),), gated=True)
    err = await store.finish(task.id, status="done", run_id=uuid4())
    assert err == "review_required"  # NOT completed (§22.1/19)
    assert await store.get(task.id) is not None  # task still exists — nothing was archived


async def test_finish_done_allowed_after_review_passes(live_pool: Any) -> None:
    task, store, _r = await _seed(
        live_pool, [{"status": "done", "verified": True}], gated=True)
    err = await store.finish(task.id, status="done", run_id=uuid4())
    assert err is None
    assert await store.get(task.id) is None  # completed + archived (§22.2)


async def test_advance_autocomplete_gated_by_review(live_pool: Any) -> None:
    # Marking the last step done does NOT auto-complete when a missing artifact fails review.
    task, store, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}, {"status": "pending"}],
        artifacts=(("/gone/app.js", "created"),), gated=True)
    updated, err = await store.advance(task.id, 2, "done", run_id=uuid4())
    assert err is None
    assert updated is not None and updated.status == "running"  # stayed running — review blocked it
    latest = await reviewer.latest_review(task.id)
    assert latest is not None and latest.status is ReviewStatus.NEEDS_REWORK


async def test_advance_autocomplete_completes_when_review_passes(live_pool: Any) -> None:
    task, store, _r = await _seed(
        live_pool, [{"status": "done", "verified": True}, {"status": "pending"}], gated=True)
    updated, err = await store.advance(task.id, 2, "done", run_id=uuid4())
    assert err is None and updated is None  # review passed → auto-completed + archived
    assert await store.get(task.id) is None


async def test_finish_task_tool_returns_structured_rejection(live_pool: Any) -> None:
    task, store, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        artifacts=(("/absent/report.pdf", "created"),), gated=True)
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool,
                      run_id=uuid4(), tasks=store, reviewer=reviewer)
    res = await FinishTask().run({"status": "done", "result": "done"}, ctx)
    assert not res.ok
    assert res.output["status"] == "rejected" and res.output["reason"] == "review_required"
    assert res.output["review_id"] and res.output["failed"]  # concrete findings for the model (§8)
    assert await store.get(task.id) is not None  # not completed


async def test_review_task_tool_reports_without_completing(live_pool: Any) -> None:
    task, store, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}], gated=True)
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool,
                      run_id=uuid4(), tasks=store, reviewer=reviewer)
    res = await ReviewTask().run({}, ctx)
    assert res.ok and res.output["status"] == "passed"
    assert await store.get(task.id) is not None  # review_task NEVER completes the task itself


# ── durability, attempts, identity (§11/§16) ────────────────────────────────────────────────────────

async def test_review_attempts_are_durable_and_incrementing(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [{"status": "pending"}])
    r1 = await reviewer.review(task.id)
    r2 = await reviewer.review(task.id)
    r3 = await reviewer.review(task.id)
    assert [r1.attempt, r2.attempt, r3.attempt] == [1, 2, 3]  # never overwritten (§22.10)
    history = await reviewer.reviews(task.id)
    assert len(history) == 3 and {h["review_id"] for h in history} == {
        str(r1.review_id), str(r2.review_id), str(r3.review_id)}


async def test_review_does_not_create_a_second_primary_task(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [{"status": "pending"}])
    await reviewer.review(task.id)
    async with live_pool.acquire() as c:
        primaries = await c.fetchval("SELECT count(*) FROM task WHERE is_primary")
        total = await c.fetchval("SELECT count(*) FROM task")
    assert primaries == 1 and total == 1  # ONE task, reviews are attached to it (§16/§22.14)


async def test_review_is_read_only_never_mutates_task_state(live_pool: Any) -> None:
    task, store, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        executions=((1, "create_file", "completed", None),))
    async with live_pool.acquire() as c:
        execs_before = await c.fetchval("SELECT count(*) FROM task_execution WHERE task_id=$1", task.id)
    await reviewer.review(task.id)
    after = await store.get(task.id)
    async with live_pool.acquire() as c:
        execs_after = await c.fetchval("SELECT count(*) FROM task_execution WHERE task_id=$1", task.id)
    # verified work is inspected, never re-executed; task/step state is untouched (§21/§22.24/29)
    assert execs_before == execs_after
    assert after is not None and after.steps[0].status == "done" and after.status == "running"


# ── events: operational state only, full identity, no chain-of-thought (§13/§30) ────────────────────

async def test_reviewer_emits_lifecycle_events_with_identity(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [{"status": "done", "verified": True}])
    run_id = uuid4()
    await reviewer.review(task.id, run_id=run_id)
    evs = await _events(live_pool, task.id)
    types = [e["type"] for e in evs]
    assert "review.started" in types and "review.passed" in types  # (§22.21)
    assert any(e["type"] == "review.requirement.checked" for e in evs)
    start = next(e for e in evs if e["type"] == "review.started")
    # full identity fields present (§22.20); review_id carried; no reasoning/thought keys (§22.30)
    assert start["payload"]["task_id"] == str(task.id)
    assert start["payload"]["run_id"] == str(run_id)
    assert "session_id" in start["payload"] and "review_id" in start["payload"]
    assert not any(k in start["payload"] for k in ("thought", "reasoning", "chain_of_thought"))


async def test_reviewer_emits_needs_rework_and_blocked_events(live_pool: Any) -> None:
    t1, _s1, rv1 = await _seed(live_pool, [{"status": "pending"}])
    await rv1.review(t1.id)
    assert "review.needs_rework" in [e["type"] for e in await _events(live_pool, t1.id)]  # (§22.22)

    t2, _s2, rv2 = await _seed(live_pool, [
        {"status": "failed", "failure_class": "permission", "last_error": "must be root"}])
    await rv2.review(t2.id)
    assert "review.blocked" in [e["type"] for e in await _events(live_pool, t2.id)]  # (§22.23)


async def test_gate_rejection_emits_completion_rejected(live_pool: Any) -> None:
    task, store, _r = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        artifacts=(("/absent/x", "created"),), gated=True)
    await store.finish(task.id, status="done", run_id=uuid4())
    rejected = await _events(live_pool, task.id, prefix="task.completion_rejected")
    assert rejected and rejected[0]["payload"]["reason"] == "review_required"


# ── continuation: rework findings survive compaction/restart (§12/§18) ──────────────────────────────

async def test_review_block_renders_deterministically_for_rework(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(
        live_pool, [{"status": "done", "verified": True}],
        artifacts=(("/missing/site.html", "created"),))
    r = await reviewer.review(task.id)
    block = continuation.render_review_block(r.to_public())
    assert "TASK REVIEW" in block and "NEEDS_REWORK" in block
    assert "site.html" in block and "Fix ONLY the failed" in block
    # a passing review injects nothing (no rework to do)
    task2, _s2, rv2 = await _seed(live_pool, [{"status": "done", "verified": True}])
    assert continuation.render_review_block((await rv2.review(task2.id)).to_public()) == ""


async def test_rework_block_reaches_the_model_on_the_next_turn(live_pool: Any) -> None:
    """Regression for the dead-code bug the audit found: the rework findings must reach the model on a
    LATER turn of the same primary task (the CONTINUED path, where the authority supplies the task note
    and the context_block() short-circuit would otherwise skip the reviewer injection). Drives a REAL
    turn end-to-end and inspects what the model actually received — not _open_tasks_note in isolation."""
    fake = FakeModelProvider(responses=[ChatResult("Continuing the build.", None, [], 3, 3, "fake")])
    loop = AgentLoop(
        pool=live_pool, provider=fake, retrieval=RetrievalService(live_pool, fake),
        context=ContextEngine(fake, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))
    pub = EventPublisher(live_pool)
    reviewer = TaskReviewer(live_pool, pub)
    loop._reviewer = reviewer                 # wire the reviewer exactly as AgentRuntime does
    loop._tasks._reviewer = reviewer
    loop._tasks._publisher = pub

    store = loop._tasks
    task = await store.create("Build the app website", ["scaffold the site"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:      # step done+verified, but the artifact is missing
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task.id)
    await store.record_artifact(task.id, "/missing/site.html", "created")
    assert (await reviewer.review(task.id)).status is ReviewStatus.NEEDS_REWORK

    await loop.run("keep going on the website build")   # a CONTINUED turn for the same primary task
    system = "\n".join(m.content for m in fake.calls[0]["messages"] if m.role == "system")
    # the deterministic rework block, re-derived from PostgreSQL, actually reached the model this turn
    assert "TASK REVIEW" in system and "FAILED REQUIREMENTS" in system
    assert "site.html" in system and "Fix ONLY the failed" in system


async def test_review_state_survives_a_simulated_restart(live_pool: Any) -> None:
    task, _s, reviewer = await _seed(live_pool, [{"status": "pending"}])
    r = await reviewer.review(task.id, run_id=uuid4())
    # a brand-new reviewer (as after a process restart) re-reads the durable verdict from PostgreSQL
    fresh = TaskReviewer(live_pool)
    latest = await fresh.latest_review(task.id)
    assert latest is not None and latest.review_id == r.review_id
    assert latest.status is ReviewStatus.NEEDS_REWORK and latest.failures  # (§22.11)


# ── the capstone: fail → detect → rework → re-review → PASS → done, one task_id (§22 integration) ────

async def test_capstone_review_rework_loop_to_completion(live_pool: Any) -> None:
    """Multi-step task, one failure introduced, reviewer detects it, rework fixes it, reviewer passes,
    task becomes done — ONE task_id, MANY run_ids, MANY review attempts, durable evidence throughout."""
    task, store, reviewer = await _seed(
        live_pool,
        [{"status": "done", "verified": True},   # step 1: scaffold — verified
         {"status": "done", "verified": True}],   # step 2: tests — marked done…
        executions=((2, "run_tests", "failed", "1 auth test failing"),),  # …but the test run FAILED
        gated=True)
    original_id = task.id
    run_ids: set[Any] = set()

    # 1) apparent completion → the gate runs the reviewer, which FINDS the failing test
    r1_run = uuid4()
    run_ids.add(r1_run)
    err = await store.finish(task.id, status="done", run_id=r1_run)
    assert err == "review_required"
    rev1 = await reviewer.latest_review(task.id)
    assert rev1 is not None and rev1.status is ReviewStatus.NEEDS_REWORK
    assert any("run_tests" in f["evidence"] for f in rev1.failures)
    assert await store.get(task.id) is not None  # task NOT completed, still primary

    # 2) REWORK (a new run): fix the failing test — record a passing execution for step 2
    r2_run = uuid4()
    run_ids.add(r2_run)
    fix = await store.record_execution(task.id, 2, "run_tests", attempt=2)
    await store.complete_execution(fix, status="completed", result_summary="all tests pass")
    async with live_pool.acquire() as c:  # the earlier failed execution is superseded by a passing one
        await c.execute("UPDATE task_execution SET status='completed' WHERE task_id=$1 AND tool_name='run_tests'",
                        task.id)

    # 3) re-review (a new run) → PASS. The history is durable WHILE the task lives (after completion it
    #    is archived to sali-works and cascade-cleaned from the DB, like executions/artifacts).
    r3_run = uuid4()
    run_ids.add(r3_run)
    rev_final = await reviewer.review(task.id, run_id=r3_run)
    assert rev_final.status is ReviewStatus.PASSED
    history = await reviewer.reviews(original_id)   # queried before archival
    assert len(history) >= 2 and history[-1]["status"] == "passed"
    assert all(h["task_id"] == str(original_id) for h in history)   # every attempt on the SAME task
    assert len({h["run_id"] for h in history}) >= 2                 # distinct review run_ids (§22.13)

    # 4) complete — the gate sees the passing review and archives; the task_id never forked (§16)
    err2 = await store.finish(task.id, status="done", run_id=r3_run)
    assert err2 is None
    assert await store.get(task.id) is None  # completed + archived
    assert len({str(x) for x in run_ids}) == 3  # multiple runs across the fail→rework→pass loop
