"""Autonomous-life capstone (§74) + adversarial recovery (§75).

Sali is running continuously with an ongoing commitment. He notices a problem → an initiative is created
→ a goal is formed → a required capability is missing → he acquires + verifies it → uses it on the
objective → context compacts → the user interrupts → the primary resumes → the process RESTARTS → the
goal, commitment, initiative, capability, and digital action are reconstructed from durable state →
external state is reconciled → he continues → the reviewer confirms the result → experience is recorded →
the goal + commitment are updated → the initiative is closed → a proactive report is emitted.

Stable throughout: goal_id, commitment_id, initiative_id, capability identity, task_id, external action
identity. No duplicate work, no lost state, no fake completion, no hallucinated capability, no abandoned
activity. Adversarial cases (restart mid-acquisition, duplicate initiative, backoff) recover deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.capability import CapabilityStore
from sali.learning.capability_acquisition import CapabilityAcquisitionStore
from sali.learning.experience import ExperienceStore
from sali.tasks.commitments import CommitmentStore
from sali.tasks.digital import DigitalActionStore
from sali.tasks.external import ExternalEntityStore
from sali.tasks.goals import GoalStore
from sali.tasks.initiatives import InitiativeStore
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db

CAP = "diagnose and repair a failing service"


def _row(r: Any) -> Any:
    assert r is not None
    return r


def _wired(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    store._experience_hook = ExperienceStore(pool, pub).extract_and_persist
    return store, ExperienceStore(pool, pub)


async def _complete_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def test_autonomous_life_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    goals, com = GoalStore(live_pool, pub), CommitmentStore(live_pool, pub)
    caps, acq = CapabilityStore(live_pool, pub), CapabilityAcquisitionStore(live_pool, pub)
    objs, acts = ExternalEntityStore(live_pool, pub), DigitalActionStore(live_pool, pub)
    inits = InitiativeStore(live_pool, pub)

    # 1) an ongoing commitment + a monitored service (a DigitalLifeObject)
    commitment = await com.create(description="Keep the staging service healthy")
    svc = await objs.discover(service="staging-svc", ref="svc-1", object_type="service", status="active")

    # 2) Sali observes a problem → an initiative is created (durable, deduped, scored)
    init_id, created = await inits.upsert_candidate(
        source="commitment", subject_ref=str(commitment), title="staging service is returning 502",
        commitment_id=commitment, priority_score=0.85, urgency_score=0.9, risk_score=0.3,
        reason_codes=["service_unhealthy"])
    assert created
    await inits.advance(init_id, "planned")

    # 3) a goal to fix it; a required capability is missing → acquire + verify it
    goal = await goals.create(objective="Restore the staging service to healthy", origin="commitment",
                              commitment_id=commitment, priority=1, next_action="diagnose the 502")
    assert not await caps.is_usable(name=CAP, scope_ref="kali")
    aid_acq = await acq.identify_gap(capability_name=CAP, scope_ref="kali")
    await acq.advance(aid_acq, "researching")
    await acq.advance(aid_acq, "acquiring")
    await acq.acquired(aid_acq)
    await caps.record_attempt(name=CAP, success=True, verified=True, scope_ref="kali")
    assert await caps.is_usable(name=CAP, scope_ref="kali")

    # 4) use the capability on the objective, in an ephemeral task workspace
    t = await store.create("Repair the staging service", ["diagnose", "fix"])
    await store.activate(t.id)
    task_id = t.id
    bound = await store.bind_workspace(t.id, objective="Repair staging", explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    action = await acts.plan(intent="restart the service and verify health", object_id=svc,
                             capability=CAP, task_id=t.id)
    await acts.advance(action, "in_progress")
    await acts.require_verification(action, expected_state="health check 200")

    # 5) COMPACTION: the life state reconstructs from durable storage
    from sali.runtime import cognitive
    snap = (await cognitive.assemble(live_pool, session_id=task_id)).snapshot()
    assert snap["active_goals"] >= 1 and snap["open_initiatives"] >= 1 and snap["commitments"] >= 1

    # 6) INTERRUPTION: suspend/resume the task — same identity
    await store.suspend(t.id, reason="quick question")
    assert _row(await store.resume(t.id)).id == task_id

    # 7) RESTART: brand-new stores reconstruct goal, commitment, initiative, capability, action
    goals2, com2, inits2 = GoalStore(live_pool), CommitmentStore(live_pool), InitiativeStore(live_pool)
    acts2, caps2 = DigitalActionStore(live_pool), CapabilityStore(live_pool)
    assert (await goals2.get(goal)) is not None
    assert (await com2.get(commitment)) is not None
    assert _row(await inits2.get(init_id))["status"] == "planned"
    assert await caps2.is_usable(name=CAP, scope_ref="kali")
    assert any(u["id"] == action for u in await acts2.unfinished())

    # 8) reconcile external state, continue, verify the action
    await objs.observe(svc, last_observed={"health": 200})
    await acts2.verify(action, observed_state="health check 200", evidence={"http": 200})
    assert _row(await acts2.get(action))["status"] == "verified"

    # 9) finish the task → reviewer PASS → experience + cleanup
    ex = await store.record_execution(t.id, 1, "systemctl_restart", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="service restarted, healthy")
    await _complete_steps(live_pool, t.id)
    assert await store.finish(t.id, status="done") is None

    # 10) update goal + commitment, close the initiative, emit a proactive report
    await goals.complete(goal, evidence={"service": "healthy"})
    await com.advance(commitment, "in_progress", next_action="continue monitoring")
    await inits.complete(init_id, evidence={"resolved_action": str(action)})
    await pub.emit(event_type="agent.message", subject_type="agent_message", origin="background",
                   data={"importance": "normal",
                         "text": "The staging service was returning 502; I restarted it and verified "
                                 "a healthy 200. I've kept the monitoring commitment open."})

    # ── assertions (§74) ────────────────────────────────────────────────────────────────────────────
    assert await store.get(task_id) is None                       # one task, completed + archived
    assert not ws.exists()                                        # ephemeral workspace cleaned
    assert _row(await goals2.get(goal))["status"] == "completed"      # same goal, completed
    assert _row(await inits2.get(init_id))["status"] == "completed"   # same initiative, closed
    assert _row(await caps2.get(name=CAP, scope="environment", scope_ref="kali"))["status"] == "verified"
    assert any("Repair the staging service" in r["content"] for r in await exp.recent())  # experience recorded
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM initiative WHERE source='commitment'") == 1  # no dup
        assert await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='agent.message'") >= 1  # proactive report emitted


# ── §75 adversarial: deterministic recovery ─────────────────────────────────────────────────────────

async def test_restart_during_capability_acquisition_resumes(live_pool: Any) -> None:
    acq = CapabilityAcquisitionStore(live_pool, EventPublisher(live_pool))
    aid = await acq.identify_gap(capability_name="use a new API", scope_ref="kali")
    await acq.advance(aid, "acquiring")                            # crash mid-acquisition
    # a brand-new store (restart) sees it in progress and can continue — never restarts from zero (§75)
    resumed = CapabilityAcquisitionStore(live_pool)
    assert any(o["id"] == aid for o in await resumed.open())
    await resumed.acquired(aid)
    assert _row(await resumed.get(aid))["status"] == "acquired"


async def test_duplicate_initiative_and_engine_rerun_are_idempotent(live_pool: Any) -> None:
    inits = InitiativeStore(live_pool)
    a, created_a = await inits.upsert_candidate(source="goal", subject_ref="g9", title="x")
    b, created_b = await inits.upsert_candidate(source="goal", subject_ref="g9", title="x (refreshed)")
    assert created_a and not created_b and a == b                 # no duplicate initiative (§14/§75)
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM initiative WHERE subject_ref='g9'") == 1


async def test_deferred_initiative_is_excluded_until_backoff_elapses(live_pool: Any) -> None:
    inits = InitiativeStore(live_pool)
    iid, _ = await inits.upsert_candidate(source="goal", subject_ref="g10", title="y")
    await inits.defer(iid, next_attempt=datetime.now(UTC) + timedelta(hours=2), reason="backoff")
    assert not any(t["id"] == iid for t in await inits.top())     # excluded while backing off (§40)
