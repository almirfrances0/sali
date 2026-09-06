"""Digital-life capstone (§53/§54) — a long-lived digital activity as a persistent life process.

Task 1: a long-lived objective needs a capability Sali lacks → he composes it from primitives →
researches → acquires → verifies → creates a DigitalLifeObject → starts an action that partially
succeeds and then needs external verification → a durable OBLIGATION is left → context compacts → the
user interrupts → the primary resumes → the process RESTARTS → Sali reconstructs the object, the open
obligation, and the action from durable state → re-observes → continues → verifies → reviewer PASS →
experience extracted → obligation closed → commitment fulfilled → ephemeral workspace cleaned.

Throughout: ONE task id, ONE digital object, ONE obligation, ONE commitment, one capability lineage —
never a duplicate created because context compacted, the process restarted, or the user interrupted.
Then §54: a LATER task benefits from the earlier verified experience + capability.
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
from sali.tasks.commitments import CommitmentStore, ObligationStore
from sali.tasks.digital import DigitalActionStore
from sali.tasks.external import ExternalEntityStore
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db

CAP = "onboard an unfamiliar web service"


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


async def test_digital_life_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    caps, acq = CapabilityStore(live_pool, pub), CapabilityAcquisitionStore(live_pool, pub)
    objs, acts = ExternalEntityStore(live_pool, pub), DigitalActionStore(live_pool, pub)
    com = CommitmentStore(live_pool, pub)

    # 1) long-lived objective + ephemeral workspace + an enduring commitment (distinct from the task)
    t = await store.create("Onboard the project onto the new service", ["onboard"])
    await store.activate(t.id)
    task_id = t.id
    bound = await store.bind_workspace(t.id, objective="Onboard service", explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    commitment = await com.create(description="Keep the service account onboarded and active", task_id=t.id)

    # 2) missing capability → compose from primitives → research → acquire → verify
    assert not await caps.is_usable(name=CAP, scope_ref="kali")
    await caps.set_dependencies(name=CAP, depends_on=["web_research", "browser", "email"], scope_ref="kali")
    aid_acq = await acq.identify_gap(capability_name=CAP, task_id=t.id, scope_ref="kali")
    await acq.advance(aid_acq, "researching", notes="read the service docs")
    await acq.advance(aid_acq, "acquiring")
    await acq.acquired(aid_acq)
    await caps.record_attempt(name=CAP, success=True, verified=True, scope_ref="kali")
    assert await caps.is_usable(name=CAP, scope_ref="kali")

    # 3) create the DigitalLifeObject + start an action that partially succeeds then needs verification
    obj = await objs.discover(service="acme-service", ref="proj-acct", object_type="account",
                              purpose="project account", task_id=t.id, status="created")
    action = await acts.plan(intent="create + verify the service account", object_id=obj,
                             capability=CAP, task_id=t.id)
    await acts.advance(action, "in_progress")
    ob_id = await acts.await_external(action, obligation_description="verify the account email",
                                      next_action="check inbox and confirm")
    assert _row(await acts.get(action))["status"] == "awaiting_external_state"

    # 4) COMPACTION: the life state reconstructs from durable storage — the obligation resurfaces
    from sali.runtime import cognitive
    snap = (await cognitive.assemble(live_pool, session_id=t.id)).snapshot()
    assert snap["open_obligations"] >= 1 and snap["digital_objects"] >= 1 and snap["commitments"] >= 1

    # 5) INTERRUPTION: suspend then resume — the task survives, same identity
    await store.suspend(t.id, reason="quick unrelated question")
    resumed = await store.resume(t.id)
    assert resumed is not None and resumed.id == task_id

    # 6) RESTART: brand-new stores reconstruct object + obligation + action from PostgreSQL alone
    objs2, acts2, obl2 = (ExternalEntityStore(live_pool), DigitalActionStore(live_pool),
                          ObligationStore(live_pool))
    assert (await objs2.get_by_id(obj)) is not None                       # object reconstructed
    assert any(o["id"] == ob_id for o in await obl2.open())               # obligation reconstructed
    assert any(u["id"] == action for u in await acts2.unfinished())       # action reconstructed

    # 7) re-observe external state, continue the action, verify → obligation auto-resolves
    stale = await objs2.is_stale(obj, max_age_seconds=1)   # remembered state may be stale (§18)
    assert isinstance(stale, bool)
    await objs2.observe(obj, last_observed={"account": "exists"})
    await acts2.resume(action)
    await acts2.verify(action, observed_state="login ok + email verified", evidence={"http": 200})
    assert _row(await acts2.get(action))["status"] == "verified"
    assert _row(await obl2.get(ob_id))["status"] == "resolved"               # unfinished business closed

    # 8) finish the task → reviewer PASS → experience + ephemeral cleanup; then fulfil the commitment
    ex = await store.record_execution(t.id, 1, "onboard_tool", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="onboarded")
    await _complete_steps(live_pool, t.id)
    assert await store.finish(t.id, status="done") is None
    await com.fulfill(commitment, evidence={"account": str(obj)})

    # ── assertions (§53) ────────────────────────────────────────────────────────────────────────────
    assert await store.get(task_id) is None                             # one task, completed + archived
    assert not ws.exists()                                              # ephemeral workspace cleaned
    assert (await objs2.get_by_id(obj)) is not None                     # the digital object survives cleanup
    assert _row(await obl2.get(ob_id))["status"] == "resolved"             # same obligation, resolved
    assert _row(await CommitmentStore(live_pool).get(commitment))["status"] == "fulfilled"  # same commitment
    assert any("Onboard the project onto the new service" in r["content"] for r in await exp.recent())
    # no duplicate task was created by compaction/restart/interruption
    async with live_pool.acquire() as c:
        # Turn 1: exactly ONE archived row (no duplicate created by compaction/restart).
        n = await c.fetchval("SELECT count(*) FROM task WHERE objective=$1 AND archived_at IS NOT NULL",
                             "Onboard the project onto the new service")
        assert n == 1


async def test_later_task_benefits_from_earlier_experience(live_pool: Any) -> None:
    # §54: a verified capability + experience from a past task makes a later, similar task easier.
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="onboard an unfamiliar web service", success=True, verified=True,
                              scope_ref="kali")
    # a NEW task with a similar objective retrieves 'I have done this before' — reuse, not from zero
    hits = await caps.for_goal("onboard our app onto another web service", scope_ref="kali")
    assert hits and hits[0]["status"] == "verified"
    assert any("onboard" in h["name"] for h in hits)
