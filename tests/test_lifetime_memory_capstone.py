"""Lifetime-memory capstone (Prompt: lifetime memory §50).

The end-to-end proof that experience is permanent while the workspace is disposable: Task A hits an
unfamiliar problem, an approach fails, a second approach succeeds and PASSes review, the experience is
distilled to durable memory, the task workspace is cleaned, the process restarts — and a later, DIFFERENT
Task B retrieves that lived experience and benefits from it, without inheriting Task A's state and without
the reviewer being bypassed. Failures and successes both survive; provenance is intact; nothing is
fabricated; no runaway duplicates are created.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.experience import EvidenceState, ExperienceStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _wired_store(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    exp = ExperienceStore(pool, pub)
    store._experience_hook = exp.extract_and_persist
    return store, exp


async def _complete_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def test_lifetime_memory_capstone(live_pool: Any) -> None:
    WORKSPACE = "/home/almir/Desktop/sali-works/deploy"

    # ── TASK A: unfamiliar problem → research → attempt A fails → attempt B succeeds → PASS ──────────
    store, exp = _wired_store(live_pool)
    a = await store.create("Deploy the Laravel application to the staging server", ["deploy"])
    await store.activate(a.id)
    task_a_id = a.id
    await store.bind_workspace(a.id, objective="Deploy Laravel to staging", explicit=None,
                              sali_works_root=WORKSPACE)
    from sali.tasks.research import ResearchStore
    await ResearchStore(live_pool).record_research(
        task_id=a.id, run_id=None, step_seq=1, query="laravel staging deploy port conflict",
        source="https://laravel.com/docs/deployment", summary="bind to an unused port", confidence=0.7,
        content_hash="h")
    fa = await store.record_execution(a.id, 1, "deploy_default_port", attempt=1)
    await store.complete_execution(fa, status="failed", error="bind failed: port 8000 already in use")
    sa = await store.record_execution(a.id, 1, "deploy_alt_port", attempt=2)
    await store.complete_execution(sa, status="completed", result_summary="served on port 8080")
    await _complete_steps(live_pool, a.id)
    assert await store.finish(a.id, status="done") is None          # reviewer PASS → hook fires

    # memory survives WORKSPACE DELETION: the task + its executions are gone, the experience remains
    assert await store.get(task_a_id) is None
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM task_execution WHERE task_id=$1", task_a_id) == 0

    # ── PROCESS RESTART: a brand-new ExperienceStore reconstructs everything from PostgreSQL ─────────
    exp2 = ExperienceStore(live_pool)
    recent = await exp2.recent()
    experience = next(r for r in recent if r["objective"] == "Deploy the Laravel application to the staging server")
    mem_id = UUID(experience["id"])

    # failed AND successful approaches both survive; provenance is intact; nothing fabricated
    prov = await exp2.provenance(mem_id)
    assert prov is not None and prov["task_id"] == str(task_a_id)          # provenance available
    assert prov["evidence_state"] == EvidenceState.VERIFIED.value          # evidence is real (reviewer PASS)
    assert prov["verification"] == "reviewer_pass" and prov["evidence"]    # not fabricated
    async with live_pool.acquire() as c:
        st = (await c.fetchrow("SELECT structured FROM memory WHERE id=$1", mem_id))["structured"]
    assert any(f["tool"] == "deploy_default_port" for f in st["failures"])  # failed approach recorded
    assert st["procedure"] == ["deploy_alt_port"]                           # successful approach recorded

    # ── TASK B: a DIFFERENT, later task with a similar problem — it RETRIEVES and benefits (§49) ─────
    b = await store.create("Deploy a Laravel app to a staging environment", ["deploy"])
    await store.activate(b.id)
    assert b.id != task_a_id                                                # different task identity

    hits = await exp2.relevant_experiences(
        objective="Deploy a Laravel app to a staging environment", limit=3)
    assert hits and any(h["id"] == str(mem_id) for h in hits)              # benefits from Task A
    assert len(hits) <= 3                                                   # retrieval is bounded
    retrieved = next(h for h in hits if h["id"] == str(mem_id))
    assert retrieved["procedure"] == ["deploy_alt_port"]                    # the reusable success
    assert any(f["tool"] == "deploy_default_port" for f in retrieved["failures"])  # and the known failure

    # Task B does NOT inherit Task A's state, and the reviewer still gates it (not bypassed)
    assert (await store.get(task_a_id)) is None
    assert (await store._reviewer.review(b.id)).status is ReviewStatus.NEEDS_REWORK

    # Task B applies the learned approach and completes on its own evidence → reviewer verifies
    eb = await store.record_execution(b.id, 1, "deploy_alt_port", attempt=1)
    await store.complete_execution(eb, status="completed", result_summary="served on port 8080")
    await _complete_steps(live_pool, b.id)
    assert (await store._reviewer.review(b.id)).status is ReviewStatus.PASSED
    assert await store.finish(b.id, status="done") is None

    # no runaway duplicates: exactly ONE experience per task (Task A's is not multiplied)
    async with live_pool.acquire() as c:
        n_a = await c.fetchval(
            "SELECT count(*) FROM memory WHERE valid_until IS NULL AND structured->>'kind'='experience' "
            "  AND structured->>'task_id'=$1", str(task_a_id))
    assert n_a == 1
