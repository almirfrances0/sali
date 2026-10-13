"""Self-preservation capstone (§29) — revocation + restart + recovery + resource pressure cannot combine
to accidentally restart work, and Sali protects the host it lives on.

A long-running task builds durable state; VRAM pressure rises so Sali compacts early and records a
resource incident; another expensive (background) activity tries to start and the resource authority
defers it under critical pressure; the user revokes the original task; every continuation/dependent thread
stops and the task becomes non-resumable; the process restarts and recovery explicitly ignores the
revoked task; it survives only as historical memory; and a later heavy plan retrieves the incident and
adapts. No background resurrection occurs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sali.events.publisher import EventPublisher
from sali.runtime.resources import (
    Priority,
    ResourceAuthority,
    ResourceBudget,
    ResourceMonitor,
    ResourceReading,
    ResourceState,
)
from sali.runtime.revocation import is_resumable, revoke_intent
from sali.tasks.commitments import CommitmentStore
from sali.tasks.goals import GoalStore
from sali.tasks.incidents import ResourceIncidentStore
from sali.tasks.revocation import RevocationStore
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


async def test_self_preservation_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    incidents = ResourceIncidentStore(live_pool, pub)
    auth = ResourceAuthority()

    # 1) a long-running task with durable state (goal + commitment + workspace + executions)
    a = await store.create("Build and train the large pipeline", ["prep", "run"])
    await store.activate(a.id)
    task_a = a.id
    await store.bind_workspace(a.id, objective="train pipeline", explicit=None,
                               sali_works_root=str(works))
    goal = await GoalStore(live_pool, pub).create(objective="finish the training run", origin="user",
                                                  task_id=a.id)
    commit = await CommitmentStore(live_pool, pub).create(description="deliver the trained model",
                                                          task_id=a.id)
    ex = await store.record_execution(a.id, 1, "prep_data", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="data prepared")

    # 2) VRAM pressure rises → Sali observes it deterministically → CRITICAL → compact + record incident
    # Read off the budget, not hardcoded: the VRAM ladder was deliberately raised (resources.py:56)
    # because sali:latest is a RESIDENT 35B on a 12 GB card whose own steady state is 90.9%, so 0.94
    # is now normal residency rather than an emergency — under the old ladder every autonomous
    # continuation turn was rejected forever.
    _b = ResourceBudget()
    mon = ResourceMonitor(reader=lambda: ResourceReading(
        vram_used_frac=_b.vram_critical, ram_used_frac=0.6, ok=True))
    assert await mon.state() is ResourceState.CRITICAL
    inc = await incidents.record(kind="vram_pressure", severity="critical",
                                 workload="large pipeline with a big context", task_id=a.id,
                                 observed={"vram_used_frac": _b.vram_critical})
    await incidents.resolve(inc, mitigation="compact early + keep a 24K bounded context")

    # 3) another expensive BACKGROUND activity tries to start → the resource authority defers it (§11/§15)
    decision = auth.decide(priority=Priority.BACKGROUND, state=await mon.state())
    assert decision.verdict == "reject"                       # curiosity/background shed under critical
    assert auth.decide(priority=Priority.USER, state=await mon.state()).verdict == "run"  # user stays served

    # 4) the user revokes the original task → tombstone + cancel all dependents + remove the live row
    result = await revoke_intent(live_pool, task_a, publisher=pub)
    assert result["revoked"]
    assert await store.get(task_a) is None                    # the live task is gone
    async with live_pool.acquire() as c:
        assert (await c.fetchval("SELECT status FROM goal WHERE id=$1", goal)) == "cancelled"
        assert (await c.fetchval("SELECT status FROM commitment WHERE id=$1", commit)) == "cancelled"

    # 5) RESTART: brand-new stores; recovery must NOT resurrect the revoked task (§24/§29)
    fresh = TaskStore(live_pool)
    assert await fresh.active_task() is None
    assert not any(x.id == task_a for x in await fresh.open_tasks())
    assert not await is_resumable(live_pool, task_a)

    # 6) the task survives ONLY as historical memory — never as current authorization (§3/§24)
    assert any(r["task_id"] == task_a for r in await RevocationStore(live_pool).recent())

    # 7) a LATER heavy plan retrieves the incident and adapts — negative operational knowledge in action (§23)
    hits = await ResourceIncidentStore(live_pool).relevant(kind="vram_pressure", workload="large pipeline")
    assert hits and "24K bounded context" in (hits[0]["mitigation"] or "")

    # 8) the invariant: revocation + restart + recovery + resource pressure never resurrected the work
    assert not await is_resumable(live_pool, task_a)
    async with live_pool.acquire() as c:      # the revoked objective is not LIVE any more
        # The row itself survives on purpose (store.py:950, "ARCHIVE-BY-STAMPING … the row STAYS
        # after snapshot — it is now the durable record"); what must not survive is anything that
        # could pick the work back up. `is_resumable` above is the load-bearing half; this checks the
        # row is stamped rather than merely present, so an unarchived leftover still fails.
        rows = await c.fetch(
            "SELECT archived_at FROM task WHERE objective='Build and train the large pipeline'")
        assert all(r["archived_at"] is not None for r in rows), (
            "a revoked objective left a LIVE task row — that is what resurrects abandoned work")
