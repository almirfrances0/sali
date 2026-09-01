"""Intent revocation, resource stewardship & self-preservation (§28).

Two invariants: the user's latest clear instruction wins — a revoked task is tombstoned, its dependent
work cancelled, and no background/recovery/restart path can resurrect it; and Sali protects the host —
resources are observed deterministically, classified into a state ladder, and a resource authority
defers/rejects work by priority so curiosity yields before the machine does. Host-endangering events
become durable negative operational knowledge. (No claim of consciousness — functional self-preservation.)
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.runtime import cognitive
from sali.runtime.resources import (
    Priority,
    ResourceAuthority,
    ResourceBudget,
    ResourceMonitor,
    ResourceReading,
    ResourceState,
    classify,
)
from sali.runtime.revocation import is_resumable, revive_intent, revoke_intent
from sali.tasks.commitments import CommitmentStore, ObligationStore
from sali.tasks.conversation import PendingQuestionStore
from sali.tasks.goals import GoalStore
from sali.tasks.incidents import ResourceIncidentStore
from sali.tasks.revocation import RevocationStore, classify_revocation
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


# ── natural-language revocation vs interruption (§1) — pure ──────────────────────────────────────────

def test_classify_revocation_is_conservative() -> None:
    for msg in ("forget that project", "leave that task for good", "cancel that project",
                "abandon it", "I don't want that anymore", "stop working on that",
                "scrap that idea"):
        assert classify_revocation(msg), msg
    # a bare interrupt / normal message is NOT a revocation (attention handles those, §1)
    for msg in ("stop", "wait", "hold on", "what's the weather?", "continue", "actually use B"):
        assert not classify_revocation(msg), msg


# ── revocation: tombstone, propagation, unresurrectable (§2/§4/§5/§24) ──────────────────────────────

async def test_revoke_tombstones_cancels_dependents_and_removes_the_task(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    t = await store.create("Build the portfolio website", ["build"])
    await store.activate(t.id)
    # dependent threads of work
    g = await GoalStore(live_pool, pub).create(objective="ship the website", origin="user", task_id=t.id)
    c = await CommitmentStore(live_pool, pub).create(description="publish the website", task_id=t.id)
    o = await ObligationStore(live_pool, pub).create(description="verify the domain", task_id=t.id)
    q = await PendingQuestionStore(live_pool, pub).ask(question="which host?", person_name="Almir",
                                                       task_id=t.id)

    result = await revoke_intent(live_pool, t.id, publisher=pub)
    assert result["revoked"] and result["cancelled"]["goal"] >= 1 and result["cancelled"]["commitment"] >= 1

    # the task is gone from all active recovery, and durably tombstoned (§4/§24)
    assert await store.get(t.id) is None and await store.active_task() is None
    assert await RevocationStore(live_pool).is_revoked(t.id)
    assert not await is_resumable(live_pool, t.id)
    # every dependent thread was cancelled — nothing left to re-authorize the work (§5)
    async with live_pool.acquire() as conn:
        for tbl, ident in (("goal", g), ("commitment", c), ("obligation", o), ("pending_question", q)):
            st = await conn.fetchval(f"SELECT status FROM {tbl} WHERE id=$1", ident)
            assert st in ("cancelled", "dismissed"), (tbl, st)


async def test_no_background_resurrection_after_restart(live_pool: Any) -> None:
    store = TaskStore(live_pool, EventPublisher(live_pool))
    t = await store.create("Build the website", ["build"])
    await store.activate(t.id)
    await revoke_intent(live_pool, t.id)
    # a brand-new store set (restart) — recovery must NOT bring the revoked task back (§24/§29)
    fresh = TaskStore(live_pool)
    assert await fresh.active_task() is None
    assert not any(x.id == t.id for x in await fresh.open_tasks())
    assert not await is_resumable(live_pool, t.id)
    # historical memory remains: the abandoned intention is still recorded (§3)
    assert any(r["task_id"] == t.id for r in await RevocationStore(live_pool).recent())


async def test_revoked_intent_returns_only_through_explicit_revival(live_pool: Any) -> None:
    store = TaskStore(live_pool, EventPublisher(live_pool))
    t = await store.create("Build the website", ["build"])
    await store.activate(t.id)
    await revoke_intent(live_pool, t.id)
    assert not await is_resumable(live_pool, t.id)
    # the user explicitly revives it → a NEW task supersedes the tombstone (§25)
    new = await store.create("Build the website (revived)", ["build"])
    await revive_intent(live_pool, t.id, new_task_id=new.id)
    assert await is_resumable(live_pool, t.id)   # the old tombstone no longer blocks


# ── resource classification + authority (§8/§9/§11/§15) — pure ──────────────────────────────────────

def test_state_is_worst_dimension() -> None:
    b = ResourceBudget()
    assert classify(ResourceReading(vram_used_frac=0.3, ram_used_frac=0.3, ok=True), b) is ResourceState.SAFE
    assert classify(ResourceReading(vram_used_frac=0.88, ok=True), b) is ResourceState.HIGH
    assert classify(ResourceReading(vram_used_frac=0.94, ok=True), b) is ResourceState.CRITICAL
    assert classify(ResourceReading(vram_used_frac=0.98, ok=True), b) is ResourceState.EMERGENCY
    # the worst dimension wins even if others are fine (92°C ≥ temp_emergency)
    assert classify(ResourceReading(vram_used_frac=0.3, gpu_temp_c=92, ok=True), b) is ResourceState.EMERGENCY


def test_authority_prioritizes_user_and_sheds_curiosity_first() -> None:
    auth = ResourceAuthority()
    # healthy → everything runs
    assert auth.decide(priority=Priority.CURIOSITY, state=ResourceState.SAFE).verdict == "run"
    # high pressure → curiosity yields, real work continues
    assert auth.decide(priority=Priority.CURIOSITY, state=ResourceState.HIGH).verdict == "defer"
    assert auth.decide(priority=Priority.CRITICAL_TASK, state=ResourceState.HIGH).verdict == "run"
    # critical → background/curiosity shed, active task deferred, user stays responsive
    assert auth.decide(priority=Priority.BACKGROUND, state=ResourceState.CRITICAL).verdict == "reject"
    assert auth.decide(priority=Priority.CRITICAL_TASK, state=ResourceState.CRITICAL).verdict == "defer"
    assert auth.decide(priority=Priority.USER, state=ResourceState.CRITICAL).verdict == "run"
    # emergency → only the user is served (§12)
    assert auth.decide(priority=Priority.CRITICAL_TASK, state=ResourceState.EMERGENCY).verdict == "reject"
    assert auth.decide(priority=Priority.USER, state=ResourceState.EMERGENCY).verdict == "run"
    assert auth.should_preserve(ResourceState.CRITICAL) and not auth.should_preserve(ResourceState.HIGH)


async def test_resource_monitor_uses_injected_reading(live_pool: Any) -> None:
    mon = ResourceMonitor(reader=lambda: ResourceReading(vram_used_frac=0.95, ram_used_frac=0.4, ok=True))
    assert await mon.state() is ResourceState.CRITICAL
    snap = await mon.snapshot()
    assert snap["state"] == "critical" and snap["preserve"] is True and snap["reading"]["ok"] is True


# ── resource incidents → negative operational knowledge that influences plans (§21/§22/§23) ─────────

async def test_resource_incident_is_durable_and_retrievable_for_planning(live_pool: Any) -> None:
    store = ResourceIncidentStore(live_pool, EventPublisher(live_pool))
    iid = await store.record(kind="gpu_oom", severity="critical",
                             workload="35B model with 64K context",
                             observed={"vram_used_frac": 0.98})
    # before repeating a similar workload, planning retrieves the relevant prior incident (§23)
    hits = await store.relevant(kind="gpu_oom", workload="35B model")
    assert hits and hits[0]["id"] == iid and hits[0]["observed"]["vram_used_frac"] == 0.98
    await store.resolve(iid, mitigation="use the 24K bounded-context strategy first")
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT resolved, mitigation FROM resource_incident WHERE id=$1", iid)
    assert row["resolved"] and "24K bounded-context" in row["mitigation"]   # reusable lesson


# ── Cognitive OS exposes self-preservation state (§27) ──────────────────────────────────────────────

async def test_cognitive_state_exposes_revocation_and_incidents(live_pool: Any) -> None:
    store = TaskStore(live_pool, EventPublisher(live_pool))
    t = await store.create("throwaway", ["x"])
    await store.activate(t.id)
    await revoke_intent(live_pool, t.id)
    await ResourceIncidentStore(live_pool).record(kind="thermal", severity="high")
    snap = (await cognitive.assemble(live_pool, session_id=uuid4())).snapshot()
    assert snap["revoked_intents"] >= 1 and snap["open_resource_incidents"] >= 1
    assert isinstance(snap["revoked_intents"], int)
