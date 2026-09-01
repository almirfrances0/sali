"""Persistent agency, completion discipline & the life loop (§56).

Sali behaves like a persistent agent: a derived life-state (never the model's say-so), completion
discipline (no permanent 'started'), a side-effect ledger that is idempotent and recoverable, a
capability model that grows only on real evidence, and a durable, safety-guarded workspace-cleanup
lifecycle that deletes ephemeral task dirs without ever touching user-owned ones — and never destroys
memory. Natural-language clarification/confirmation is durable state, not a y/n gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.capability import CapabilityStore
from sali.learning.experience import ExperienceStore
from sali.runtime import cognitive
from sali.tasks.activity import ActivityStore
from sali.tasks.cleanup import WorkspaceCleanupStore
from sali.tasks.coordination import QuestionStore
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.side_effects import SideEffectStore
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _wired(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    exp = ExperienceStore(pool, pub)
    store._experience_hook = exp.extract_and_persist
    return store, exp


async def _complete_steps(pool: Any, task_id: Any) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


# ── life state: derived, reconstructable, not reset by a new conversation (§3/§5/§42) ────────────────

async def test_cognitive_state_derives_life_mode_and_activity(live_pool: Any) -> None:
    store, _ = _wired(live_pool)
    t = await store.create("Build a service", ["scaffold"])
    await store.activate(t.id)
    await ActivityStore(live_pool).start(task_id=t.id, kind="inspect", description="inspect the project")
    state = await cognitive.assemble(live_pool, loop=_LoopStub(store), session_id=uuid4())
    snap = state.snapshot()
    assert snap["life_mode"] == "working"                       # derived from durable state, not the model
    assert snap["current_activity"] and snap["current_activity"]["kind"] == "inspect"
    assert "capability_count" in snap and "unfinished_side_effects" in snap


async def test_new_conversation_does_not_reset_task_or_life_state(live_pool: Any) -> None:
    store, _ = _wired(live_pool)
    t = await store.create("Long project", ["a"])
    await store.activate(t.id)
    # a "new conversation" = a brand-new loop/store graph; the life state reconstructs from PostgreSQL
    state = await cognitive.assemble(live_pool, loop=_LoopStub(TaskStore(live_pool)), session_id=uuid4())
    assert state.snapshot()["task_id"] == str(t.id)            # task survives the new conversation (§18)
    assert state.life_mode in ("working", "waiting")


# ── completion discipline: every started activity reaches a terminal state (§6) ─────────────────────

async def test_activity_reaches_terminal_state_and_preserves_reason(live_pool: Any) -> None:
    acts = ActivityStore(live_pool, EventPublisher(live_pool))
    a1 = await acts.start(task_id=None, kind="install", description="install deps")
    a2 = await acts.start(task_id=None, kind="test", description="run tests")
    a3 = await acts.start(task_id=None, kind="deploy", description="deploy")
    await acts.complete(a1)
    await acts.fail(a2, error="2 tests failed")
    await acts.block(a3, reason="needs a credential")
    assert not await acts.open_activities(task_id=None) or \
        all(o["status"] in ("blocked", "deferred", "waiting_for_user") for o in await acts.open_activities())
    async with live_pool.acquire() as c:
        rows = {r["id"]: r for r in await c.fetch("SELECT id, status, error FROM activity")}
    assert rows[a1]["status"] == "completed"
    assert rows[a2]["status"] == "failed" and "2 tests failed" in rows[a2]["error"]   # reason preserved
    assert rows[a3]["status"] == "blocked" and "credential" in rows[a3]["error"]       # blocker preserved


async def test_blocked_activity_resumes(live_pool: Any) -> None:
    acts = ActivityStore(live_pool)
    a = await acts.start(task_id=None, kind="deploy")
    await acts.block(a, reason="waiting on DNS")
    await acts.resume(a)                                        # interruption → resume (§19)
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT status FROM activity WHERE id=$1", a)
    assert row["status"] == "started"


# ── side effects: durable, idempotent, recoverable, restart-visible (§7/§8/§53) ─────────────────────

async def test_side_effect_is_durable_and_not_repeated(live_pool: Any) -> None:
    se = SideEffectStore(live_pool, EventPublisher(live_pool))
    sid, already = await se.plan(kind="created", target="account: staging", idempotency_key="acct:staging")
    assert not already
    await se.attempt(sid)
    await se.succeed(sid, after_state="account active")
    # planning the SAME effect again is a no-op — it is not repeated (§53)
    sid2, already2 = await se.plan(kind="created", target="account: staging", idempotency_key="acct:staging")
    assert already2 and sid2 == sid
    assert (await se.already_done("acct:staging")) is not None


async def test_failed_side_effect_is_recoverable_and_restart_visible(live_pool: Any) -> None:
    se = SideEffectStore(live_pool)
    sid, _ = await se.plan(kind="registered", target="external resource")
    await se.attempt(sid)
    # a brand-new store (restart) sees the unfinished effect
    assert any(u["id"] == sid for u in await SideEffectStore(live_pool).unfinished())
    await se.fail(sid, error="verification timed out")
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT status FROM side_effect WHERE id=$1", sid))["status"] == "failed"


# ── capability foundation: representable, evidence-backed, scoped, no hardcoding (§12-16) ────────────

async def test_unknown_capability_is_representable_and_references_support(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool, EventPublisher(live_pool))
    # a capability nobody hardcoded — just a name + supporting references
    cid = await caps.observe(name="deploy a Rust axum service", scope="environment", scope_ref="kali",
                             status="researched",
                             supported_by={"skills": ["rust"], "research": ["axum docs"]})
    got = await caps.get(name="deploy a Rust axum service", scope="environment", scope_ref="kali")
    assert got is not None and got["status"] == "researched"
    assert got["supported_by"]["skills"] == ["rust"]           # references skill/research (§15)
    assert str(cid)


async def test_failed_attempt_is_negative_knowledge_verified_attempt_gains_evidence(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="configure nginx", success=False, scope_ref="kali")
    failed = await caps.get(name="configure nginx", scope="environment", scope_ref="kali")
    assert failed is not None and failed["status"] != "verified" and failed["times_failed"] == 1  # §14
    await caps.record_attempt(name="configure nginx", success=True, verified=True, scope_ref="kali")
    ok = await caps.get(name="configure nginx", scope="environment", scope_ref="kali")
    assert ok is not None and ok["status"] == "verified" and ok["confidence"] > failed["confidence"]


async def test_capability_scope_is_preserved(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="build docker image", success=True, verified=True, scope_ref="kali")
    assert await caps.get(name="build docker image", scope="environment", scope_ref="kali") is not None
    # the SAME capability name on a different environment is a distinct, un-verified capability (§16)
    assert await caps.get(name="build docker image", scope="environment", scope_ref="ubuntu") is None


# ── workspace cleanup: ephemeral deleted, user-owned kept, failure durable+resumable (§24-26) ────────

async def test_ephemeral_workspace_deleted_but_memory_survives(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    store, exp = _wired(live_pool)
    t = await store.create("Deploy the app to staging", ["deploy"])
    await store.activate(t.id)
    bound = await store.bind_workspace(t.id, objective="Deploy the app", explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    assert ws.exists() and bound["mode"] == "auto"
    (ws / "artifact.txt").write_text("built")
    ex = await store.record_execution(t.id, 1, "deploy_tool", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="deployed")
    await _complete_steps(live_pool, t.id)
    assert await store.finish(t.id, status="done") is None
    assert not ws.exists()                                     # ephemeral workspace cleaned up (§24)
    assert any("Deploy the app to staging" in r["content"] for r in await exp.recent())  # memory survives (§27)
    cu = await WorkspaceCleanupStore(live_pool).for_task(t.id)
    assert cu is not None and cu["status"] == "completed" and cu["workspace_type"] == "ephemeral"


async def test_user_owned_workspace_is_not_deleted(live_pool: Any, tmp_path: Path) -> None:
    userdir = tmp_path / "my-real-project"
    userdir.mkdir()
    (userdir / "keep.txt").write_text("mine")
    store, _ = _wired(live_pool)
    t = await store.create("Work in the user's project", ["a"])
    await store.activate(t.id)
    bound = await store.bind_workspace(t.id, objective="Work in project", explicit=str(userdir),
                                       sali_works_root=str(tmp_path / "sali-works"))
    assert bound["mode"] == "explicit"
    await _complete_steps(live_pool, t.id)
    assert await store.finish(t.id, status="done") is None
    assert userdir.exists() and (userdir / "keep.txt").exists()   # user-owned NEVER auto-deleted (§25)
    cu = await WorkspaceCleanupStore(live_pool).for_task(t.id)
    assert cu is not None and cu["status"] == "skipped" and cu["workspace_type"] == "user_owned"


async def test_cleanup_failure_is_durable_and_resumable(live_pool: Any, tmp_path: Path,
                                                        monkeypatch: Any) -> None:
    works = tmp_path / "sali-works"
    store, _ = _wired(live_pool)
    t = await store.create("Task with a stubborn workspace", ["a"])
    await store.activate(t.id)
    bound = await store.bind_workspace(t.id, objective="x", explicit=None, sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    cu = WorkspaceCleanupStore(live_pool)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("device busy")
    monkeypatch.setattr("sali.tasks.cleanup.shutil.rmtree", _boom)
    res = await cu.cleanup_task(t.id)
    assert res["status"] == "failed" and ws.exists()          # not deleted; recorded durably, not faked (§26)
    assert any(p["task_id"] == t.id for p in await cu.pending())   # visible as resumable

    monkeypatch.undo()
    assert await cu.resume_pending() >= 1                      # a later pass finishes it (§52)
    assert not ws.exists()


# ── natural-language clarification is durable state, not a y/n gate (§10/§11/§36) ────────────────────

async def test_clarification_answer_is_natural_and_durable(live_pool: Any) -> None:
    store = TaskStore(live_pool, EventPublisher(live_pool))
    t = await store.create("Deploy somewhere", ["deploy"])
    await store.activate(t.id)
    q = QuestionStore(live_pool, EventPublisher(live_pool))
    await q.ask(t.id, "I can deploy to staging or prod — which do you want?")
    # a natural-language answer (not y/n) is accepted and stored durably
    assert await q.answer(t.id, "actually, let's do staging for now") is True
    async with live_pool.acquire() as c:  # a brand-new session still sees the durable answer (§11 no re-ask)
        row = await c.fetchrow(
            "SELECT answer, status FROM task_question WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", t.id)
    assert row["status"] == "answered" and "staging" in row["answer"]
    resumed = await store.get(t.id)
    assert resumed is not None and resumed.status == "running"   # the task resumed, same task (§11)


class _LoopStub:
    """Minimal loop stand-in for cognitive.assemble — just enough for the life-state derivation."""

    def __init__(self, store: TaskStore) -> None:
        self._tasks = store
        self._reviewer = None
        self._research_store = None
        self._skills = None
        self._decisions = None
        self._phases = None

        class _SelfState:
            async def assemble(self_inner: Any) -> dict[str, Any]:
                return {}
        self._self_state = _SelfState()

        class _Registry:
            def advertise(self_inner: Any) -> list[Any]:
                return []
        self.registry = _Registry()
