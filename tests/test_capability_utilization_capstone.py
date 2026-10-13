"""Capability-utilization capstone (§33) — learn once, retain the usable result, benefit later.

Task A hits an unfamiliar capability → checks existing (none) → real gap → research → practice → FAILS
once (negative knowledge) → adjusts → succeeds → verifies → the capability is registered as available →
task completes → ephemeral workspace cleaned. Process restarts. Task B (a later, unrelated task) needs
the SAME capability → Sali DISCOVERS the prior verified capability, does NOT relearn from zero, USES it,
the use succeeds, and usage history updates. Proves: what Sali learned once changed what he can do, and
he benefited from it later.
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

CAP = "automate the widget pipeline"


def _wired(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    store._experience_hook = ExperienceStore(pool, pub).extract_and_persist
    return store, ExperienceStore(pool, pub)


async def _complete_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def test_capability_utilization_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    caps, acq = CapabilityStore(live_pool, pub), CapabilityAcquisitionStore(live_pool, pub)

    # ── TASK A: unfamiliar capability → gap → research → fail once → adjust → succeed → register ─────
    a = await store.create("Build the widget automation", ["automate"])
    await store.activate(a.id)
    bound = await store.bind_workspace(a.id, objective="automate", explicit=None, sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    assert not await caps.already_have(name=CAP, scope_ref="kali")     # genuine gap (§16 checked first)

    aid = await acq.identify_gap(capability_name=CAP, scope_ref="kali")
    await acq.advance(aid, "researching")
    await acq.advance(aid, "acquiring")
    await caps.record_attempt(name=CAP, success=False, scope_ref="kali")   # first attempt FAILS (negative)
    failed = await caps.get(name=CAP, scope="environment", scope_ref="kali")
    assert failed is not None and failed["status"] != "verified" and failed["times_failed"] == 1
    await caps.record_attempt(name=CAP, success=True, verified=True, scope_ref="kali")   # adjusted → succeeds
    await acq.acquired(aid)
    await caps.set_purpose(name=CAP, purpose="build widget pipelines for future projects", scope_ref="kali")
    assert await caps.check_availability(name=CAP, scope_ref="kali") == "available"

    # use it for the objective, finish → reviewer PASS → experience + cleanup
    await caps.record_use(name=CAP, scope_ref="kali", action="ran the pipeline", success=True, verified=True,
                          task_id=a.id)
    ex = await store.record_execution(a.id, 1, "automate_tool", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="pipeline built")
    await _complete_steps(live_pool, a.id)
    assert await store.finish(a.id, status="done") is None
    assert not ws.exists()                                            # ephemeral workspace cleaned (§19/§20)

    # ── RESTART: the usable capability + its history survive (§20/§32) ───────────────────────────────
    caps2 = CapabilityStore(live_pool)
    assert await caps2.already_have(name=CAP, scope_ref="kali")
    got = await caps2.get(name=CAP, scope="environment", scope_ref="kali")
    assert got is not None and "future projects" in got["purpose"] and got["use_count"] == 1
    # the failure is retained as negative knowledge, not erased
    assert got["times_failed"] == 1 and got["times_succeeded"] >= 1

    # ── TASK B (later): needs the SAME capability → discover + reuse, DO NOT relearn from zero (§8/§22) ─
    b = await store.create("Set up another widget automation", ["automate"])
    await store.activate(b.id)
    assert b.id != a.id
    hits = await caps2.discover_for_task("set up widget automation for this project", scope_ref="kali")
    assert any(h["name"] == CAP and h["status"] in ("verified", "available") for h in hits)
    async with live_pool.acquire() as c:  # NO new acquisition was started — he reused, not relearned (§16)
        n_acq = await c.fetchval("SELECT count(*) FROM capability_acquisition WHERE capability_name=$1", CAP)
    assert n_acq == 1

    # use it again → usage history grows (§12/§27 repeated successful use strengthens competence)
    assert await caps2.record_use(name=CAP, scope_ref="kali", action="ran the pipeline again",
                                  success=True, verified=True, task_id=b.id)
    final = await caps2.get(name=CAP, scope="environment", scope_ref="kali")
    assert final is not None and final["use_count"] == 2
    hist = await caps2.usage_history(name=CAP, scope_ref="kali")
    assert len(hist) == 2 and all(h["success"] for h in hist)

    # finish task B on its own evidence (reviewer still gates it)
    await _complete_steps(live_pool, b.id)
    assert (await store._reviewer.review(b.id)).status is ReviewStatus.PASSED
    assert await store.finish(b.id, status="done") is None
