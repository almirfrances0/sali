"""The Sali life-loop capstone (§57) — one serious end-to-end run.

A Laravel task in an ephemeral workspace: inspect → work → hit an unknown → research → apply → an
attempt fails then a second succeeds → a consequential side effect is completed → the task is
interrupted and resumed → the process 'restarts' and reconstructs its life state from durable storage →
verification → reviewer PASS → experience extracted → capability evidence updated → memory consolidated
→ the ephemeral workspace is physically deleted → cleanup verified → task archived. The whole time there
is one task identity, evidence-based completion, and no abandoned side effects — and after the files are
gone, Sali still remembers what happened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.capability import CapabilityStore
from sali.learning.experience import ExperienceStore
from sali.runtime import cognitive
from sali.tasks.activity import ActivityStore
from sali.tasks.cleanup import WorkspaceCleanupStore
from sali.tasks.research import ResearchStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.side_effects import SideEffectStore
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _wired(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    store._experience_hook = ExperienceStore(pool, pub).extract_and_persist  # also derives capability
    return store, ExperienceStore(pool, pub)


async def _complete_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


class _LoopStub:
    def __init__(self, store: TaskStore) -> None:
        self._tasks, self._reviewer = store, None
        self._research_store = self._skills = self._decisions = self._phases = None

        class _S:
            async def assemble(self_i: Any) -> dict[str, Any]:
                return {}
        self._self_state = _S()

        class _R:
            def advertise(self_i: Any) -> list[Any]:
                return []
        self.registry = _R()


async def test_life_loop_capstone(live_pool: Any, tmp_path: Path) -> None:
    OBJECTIVE = "Build a Laravel application with authentication"
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    acts = ActivityStore(live_pool, pub)
    se = SideEffectStore(live_pool, pub)

    # 1) task + ephemeral workspace + activities (completion discipline)
    t = await store.create(OBJECTIVE, ["scaffold", "auth"])
    await store.activate(t.id)
    task_id = t.id
    bound = await store.bind_workspace(t.id, objective=OBJECTIVE, explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    assert ws.exists() and bound["mode"] == "auto"
    (ws / "composer.json").write_text("{}")

    a_inspect = await acts.start(task_id=t.id, kind="inspect", description="inspect environment")
    await acts.complete(a_inspect)

    # 2) hit an unknown → research → persist it
    a_research = await acts.start(task_id=t.id, kind="research", description="auth scaffolding")
    await ResearchStore(live_pool, pub).record_research(
        task_id=t.id, run_id=None, step_seq=2, query="Laravel Breeze auth",
        source="https://laravel.com/docs/breeze", summary="composer require laravel/breeze --dev",
        confidence=0.8, content_hash="h")
    await acts.complete(a_research)

    # 3) apply: an attempt fails, a second succeeds (recovery); a consequential side effect completes
    a_apply = await acts.start(task_id=t.id, kind="apply", description="install + configure auth")
    f = await store.record_execution(t.id, 2, "composer_require_wrong", attempt=1)
    await store.complete_execution(f, status="failed", error="package not found: breze")
    ok = await store.record_execution(t.id, 2, "composer_require_breeze", attempt=2)
    await store.complete_execution(ok, status="completed", result_summary="breeze installed")
    sid, _ = await se.plan(kind="installed", target="laravel/breeze", task_id=t.id,
                           idempotency_key=f"install:breeze:{t.id}")
    await se.attempt(sid)
    await se.succeed(sid, after_state="breeze present in vendor/")
    await acts.complete(a_apply)

    # 4) INTERRUPTION: suspend then resume — the task survives, same identity (§19)
    await store.suspend(t.id, reason="quick unrelated question")
    resumed = await store.resume(t.id)
    assert resumed is not None and resumed.id == task_id

    # 5) COMPACTION/RESTART: brand-new object graph reconstructs the life state from PostgreSQL alone
    state = await cognitive.assemble(live_pool, loop=_LoopStub(TaskStore(live_pool)), session_id=uuid4())
    snap = state.snapshot()
    assert snap["task_id"] == str(task_id)                     # task identity preserved across restart
    assert snap["life_mode"] in ("working", "waiting")
    assert snap["unfinished_side_effects"] == 0                # the install side effect was completed

    # 6) verification step + reviewer PASS → experience/capability/memory + ephemeral cleanup
    v = await store.record_execution(t.id, 1, "verify_app", attempt=1)
    await store.complete_execution(v, status="completed", result_summary="app boots")
    await _complete_steps(live_pool, task_id)
    assert (await store._reviewer.review(task_id)).status is ReviewStatus.PASSED
    assert await store.finish(task_id, status="done") is None

    # ── assertions (§57) ────────────────────────────────────────────────────────────────────────────
    assert await store.get(task_id) is None                   # task completed + archived
    assert not ws.exists()                                     # ephemeral workspace physically deleted

    experience = next(r for r in await exp.recent() if r["objective"] == OBJECTIVE)
    mem_id = UUID(experience["id"])
    prov = await exp.provenance(mem_id)
    assert prov is not None and prov["task_id"] == str(task_id) and prov["evidence_state"] == "verified"
    assert prov["research"]                                    # research history preserved in the experience
    async with live_pool.acquire() as c:
        st = (await c.fetchrow("SELECT structured FROM memory WHERE id=$1", mem_id))["structured"]
        proc = await c.fetchrow(
            "SELECT structured FROM memory WHERE layer='procedural' AND valid_until IS NULL "
            "  AND structured->>'source_task'=$1", str(task_id))
    assert any(f["tool"] == "composer_require_wrong" for f in st["failures"])   # failure kept (negative)
    assert st["procedure"] and "composer_require_wrong" not in st["procedure"]  # only the successful path
    assert proc is not None                                    # verified procedure → procedural memory

    cap = await CapabilityStore(live_pool).get(
        name=OBJECTIVE[:120], scope="environment", scope_ref=bound["workspace_root"])
    assert cap is not None and cap["status"] == "verified" and cap["times_succeeded"] >= 1  # capability evidence

    cu = await WorkspaceCleanupStore(live_pool).for_task(task_id)
    assert cu is not None and cu["status"] == "completed"      # cleanup verified

    assert not await SideEffectStore(live_pool).unfinished()   # no side effects left unfinished
