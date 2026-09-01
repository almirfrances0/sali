"""Capability-evolution capstone (§69/§70/§71) — the whole agentic loop, end to end.

Task 1: Sali finds he lacks a capability → identifies the gap → researches → acquires → verifies →
uses it for the real objective → an ephemeral workspace is cleaned after a reviewer PASS → the
experience and the capability survive. Restart. Task 2: a new objective needs the SAME capability →
Sali retrieves 'I have done this before' and reuses it. Regression: the capability's dependency
disappears → Sali detects it is no longer usable (without erasing history) → recovers it.

One capability identity, one task identity per task, evidence-based verification throughout, and no
false claims — a failed step never becomes a capability, and 'done' requires the reviewer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.capability import CapabilityStore
from sali.learning.capability_acquisition import CapabilityAcquisitionStore
from sali.learning.experience import ExperienceStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db

CAP = "deploy a Laravel application"


def _wired(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    store._experience_hook = ExperienceStore(pool, pub).extract_and_persist
    return store, ExperienceStore(pool, pub)


async def _complete_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def test_capability_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    caps = CapabilityStore(live_pool, pub)
    acq = CapabilityAcquisitionStore(live_pool, pub)

    # ── TASK 1: identify the capability gap and acquire it ──────────────────────────────────────────
    t1 = await store.create("Deploy the Laravel app to staging", ["deploy"])
    await store.activate(t1.id)
    task1 = t1.id
    bound = await store.bind_workspace(t1.id, objective="Deploy Laravel", explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    assert not await caps.is_usable(name=CAP, scope_ref="kali")   # Sali cannot do this yet

    aid = await acq.identify_gap(capability_name=CAP, missing=[CAP], task_id=t1.id, scope_ref="kali")
    await acq.advance(aid, "researching", notes="read Laravel deployment docs")
    await acq.advance(aid, "acquiring")
    await acq.advance(aid, "verifying")
    await acq.acquired(aid)
    # the capability becomes REAL only via verified evidence, not because acquisition finished (§59)
    await caps.record_attempt(name=CAP, success=True, verified=True, scope="environment", scope_ref="kali")
    assert await caps.is_usable(name=CAP, scope_ref="kali")       # now Sali can do it

    # ── use it for the real objective → reviewer PASS → experience + cleanup ─────────────────────────
    ex = await store.record_execution(t1.id, 1, "deploy_tool", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="deployed to staging")
    await _complete_steps(live_pool, t1.id)
    assert await store.finish(t1.id, status="done") is None

    assert await store.get(task1) is None                         # task 1 completed + archived
    assert not ws.exists()                                        # ephemeral workspace cleaned (§46/§47)
    assert any("Deploy the Laravel app to staging" in r["content"] for r in await exp.recent())  # experience survives
    got = await acq.get(aid)
    assert got is not None and got["status"] == "acquired"        # acquisition has durable identity (§69)

    # ── RESTART: brand-new stores reconstruct capability + acquisition + experience from PostgreSQL ──
    caps2 = CapabilityStore(live_pool)
    assert await caps2.is_usable(name=CAP, scope_ref="kali")

    # ── TASK 2 (§70): a new objective needs the SAME capability → reuse 'I've done this before' ──────
    t2 = await store.create("Deploy another Laravel app to staging", ["deploy"])
    await store.activate(t2.id)
    assert t2.id != task1
    hits = await caps2.for_goal("Deploy another Laravel app to staging", scope_ref="kali")
    # 'I have done this before' — the acquired capability is retrieved and reusable, top hit verified
    assert hits and hits[0]["status"] == "verified" and any(h["name"] == CAP for h in hits)
    # the reviewer is NOT bypassed for task 2 (still evidence-based, §15/§59)
    assert (await store._reviewer.review(t2.id)).status is ReviewStatus.NEEDS_REWORK
    ex2 = await store.record_execution(t2.id, 1, "deploy_tool", attempt=1)
    await store.complete_execution(ex2, status="completed", result_summary="deployed")
    await _complete_steps(live_pool, t2.id)
    assert await store.finish(t2.id, status="done") is None
    # using it again strengthens the capability (more evidence)
    await caps2.record_attempt(name=CAP, success=True, verified=True, scope_ref="kali")
    strengthened = await caps2.get(name=CAP, scope="environment", scope_ref="kali")
    assert strengthened is not None and strengthened["times_succeeded"] >= 2

    # ── REGRESSION (§71): the dependency disappears → detected as unusable, history preserved ────────
    assert await caps2.degrade(name=CAP, scope_ref="kali", reason="PHP runtime removed")
    assert not await caps2.is_usable(name=CAP, scope_ref="kali")   # Sali does NOT hallucinate it still works
    degraded = await caps2.get(name=CAP, scope="environment", scope_ref="kali")
    assert degraded is not None and degraded["status"] == "degraded" and degraded["times_succeeded"] >= 2
    # recover it → usable again, history intact and appended
    assert await caps2.restore(name=CAP, scope_ref="kali")
    assert await caps2.is_usable(name=CAP, scope_ref="kali")
    recovered = await caps2.get(name=CAP, scope="environment", scope_ref="kali")
    assert recovered is not None and recovered["times_succeeded"] >= 3   # history preserved + appended
