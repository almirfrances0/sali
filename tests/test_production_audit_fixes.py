"""Regression tests for the Final production-readiness audit fixes.

Each test pins a specific defect the audit found and this pass fixed, so it can never silently regress.
Grouped by the audit dimension. DB-backed tests use live_pool; pure ones need no DB.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, PermissionsSettings, Settings
from sali.context.engine import ContextEngine
from sali.provider.fake import FakeModelProvider
from sali.retrieval.models import RetrievalBundle

# ── security-injection: secrets are unreadable by Sali's own tools (§28) ─────────────────────────────

def test_config_sali_is_denied_by_pathguard() -> None:
    from sali.tools.pathguard import PathGuard, PathViolation

    guard = PathGuard(PermissionsSettings())
    vault_key = str(Path.home() / ".config" / "sali" / "vault.key")
    api_token = str(Path.home() / ".config" / "sali" / "api_token")
    for p in (vault_key, api_token):
        with pytest.raises(PathViolation):
            guard.check_read(p)


def test_ssh_option_smuggling_is_rejected() -> None:
    from sali.tools.remote import _reject_option_smuggling

    _reject_option_smuggling("example.com", "/tmp/file")           # ordinary values are fine
    with pytest.raises(ValueError):
        _reject_option_smuggling("-oProxyCommand=touch /tmp/pwned")  # option-looking host is refused
    with pytest.raises(ValueError):
        _reject_option_smuggling("host", "-oProxyCommand=x")         # option-looking local path is refused


# ── security-authz: query-string tokens are not accepted on REST (§28) ───────────────────────────────

def test_bearer_token_ignores_query_string() -> None:
    from typing import cast

    from fastapi import Request

    from sali.api.auth import bearer_token

    class _Req:
        def __init__(self, headers: dict[str, str], qp: dict[str, str]) -> None:
            self.headers = headers
            self.query_params = qp

    assert bearer_token(cast("Request", _Req({"authorization": "Bearer abc"}, {}))) == "abc"
    # a token supplied only via ?token= is NOT honored for REST (it would leak into logs)
    assert bearer_token(cast("Request", _Req({}, {"token": "leaky"}))) is None


# ── context-compaction: the compacted summary is never dropped from the prompt (§9) ──────────────────

def test_compacted_summary_survives_ten_recent_messages() -> None:
    engine = ContextEngine(FakeModelProvider(), ctx_tokens=8192)
    # An ("earlier", …) summary prepended by _load_history, then 10 recent messages — the old history[-10:]
    # slice would have dropped the summary at index 0.
    history: list[tuple[str, str]] = [("earlier", "SUMMARY: building the Laravel app; NEXT: run tests")]
    history += [("user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(10)]
    ctx = engine.assemble("continue", RetrievalBundle(), [], history=history)
    text = "\n".join(m.content or "" for m in ctx.messages)
    assert "SUMMARY: building the Laravel app" in text  # the compacted operational state is preserved


pytestmark = pytest.mark.db  # everything below needs Postgres


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


# ── completion-authority: a zero-requirement task must NOT pass vacuously (§25) ──────────────────────

async def test_zero_requirement_task_does_not_pass_review(live_pool: Any) -> None:
    from sali.events.publisher import EventPublisher
    from sali.tasks.reviewer import ReviewStatus, TaskReviewer
    from sali.tasks.store import TaskStore

    store = TaskStore(live_pool, EventPublisher(live_pool))
    store._reviewer = TaskReviewer(live_pool)   # production always wires the reviewer (bare store is permissive)
    task = await store.create("revived objective", [])   # zero steps, zero artifacts
    await store.activate(task.id)
    verdict = await TaskReviewer(live_pool).review(task.id)
    assert verdict.status is ReviewStatus.NEEDS_REWORK  # nothing verified → never a vacuous PASS

    # …and finish('done') is refused for it (the reviewer gate blocks completion)
    err = await store.finish(task.id, status="done")
    assert err == "review_required"
    still = await store.get(task.id)
    assert still is not None and still.status != "done"


# ── source-of-truth: mid-plan step insert works (invalid UPDATE...ORDER BY fixed) (§4) ───────────────

async def test_modify_task_insert_mid_plan_is_collision_free(live_pool: Any) -> None:
    from sali.core.clock import SystemClock
    from sali.events.publisher import EventPublisher
    from sali.tasks.store import TaskStore
    from sali.tools.builtins.task_tool import ModifyTask
    from sali.tools.context import ToolContext

    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("build", ["step one", "step two", "step three"])
    await store.activate(task.id)
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, tasks=store,
                      run_id=uuid4())
    res = await ModifyTask().run(
        {"action": "add", "after_step": 1, "description": "inserted step"}, ctx)
    assert res.ok, res.error
    async with live_pool.acquire() as c:
        rows = await c.fetch(
            "SELECT seq, description FROM task_step WHERE task_id=$1 ORDER BY seq", task.id)
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(set(seqs)) and len(seqs) == 4       # no collision, no duplicate seq
    assert rows[1]["description"] == "inserted step"          # inserted right after step 1


# ── concurrency: crash-recovery claims a stale run atomically (no double re-drive) (§34) ─────────────

async def test_stale_run_is_claimed_atomically_once(live_pool: Any) -> None:
    # Simulate two processes racing to reclaim the same stale 'running' run: the conditional claim
    # (WHERE status='running' RETURNING) must succeed exactly once.
    run_id = uuid4()
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO agent_runs (run_id, session_id, user_input, state, status, updated_at) "
            "VALUES ($1,$2,$3,'execute_tool','running', now() - interval '5 minutes')",
            run_id, uuid4(), "do the thing")
    claim_sql = ("UPDATE agent_runs SET status='aborted', updated_at=now() "
                 "WHERE run_id=$1 AND status='running' RETURNING run_id")
    async with live_pool.acquire() as c1:
        first = await c1.fetchval(claim_sql, run_id)
    async with live_pool.acquire() as c2:
        second = await c2.fetchval(claim_sql, run_id)
    assert first is not None and second is None   # exactly one process wins the claim


# ── security-authz: a revoked device's live WebSocket is torn down (§28) ─────────────────────────────

async def test_ws_disconnect_device_and_reap_invalid(live_pool: Any) -> None:
    from sali.api.devices import DeviceStore
    from sali.api.ws import ConnectionManager
    from tests.apiutil import FakeWebSocket

    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None
    dev = str(session.device_id)

    mgr = ConnectionManager()
    ws = FakeWebSocket()
    cid = uuid4()
    assert await mgr.connect(ws, cid, device_id=dev, session_id=None)  # type: ignore[arg-type]
    # revoke → disconnect_device closes the bound socket
    await store.revoke_device(session.device_id)
    closed = await mgr.disconnect_device(dev)
    assert closed == 1 and ws.closed_code == 4001 and mgr.active_count == 0

    # reap_invalid closes a socket whose session is revoked/expired
    session2 = await store.redeem_enrollment(
        (await store.mint_enrollment_code())[0], name="iPhone2")
    assert session2 is not None
    ws2 = FakeWebSocket()
    cid2 = uuid4()
    sid = None
    async with live_pool.acquire() as c:
        sid = str(await c.fetchval(
            "SELECT id FROM device_session WHERE device_id=$1", session2.device_id))
        # force the session to look expired
        await c.execute("UPDATE device_session SET access_expires_at = now() - interval '1 minute' "
                        "WHERE id=$1", sid)
    assert await mgr.connect(ws2, cid2,  # type: ignore[arg-type]
                             device_id=str(session2.device_id), session_id=sid)
    reaped = await mgr.reap_invalid(live_pool)
    assert reaped == 1 and ws2.closed_code == 4001


# ── cancellation: an explicit "stop that" durably revokes the active task (§5/§6) ────────────────────

async def test_cancel_phrase_revokes_active_task(live_pool: Any) -> None:
    from uuid import uuid4 as _uuid4

    from sali.context.engine import ContextEngine
    from sali.provider.base import ChatResult
    from sali.retrieval.service import RetrievalService
    from sali.runtime.loop import AgentLoop
    from sali.runtime.revocation import is_resumable
    from sali.runtime.runtime import AgentRuntime
    from sali.security.confirm import AutoAllowConfirmer
    from sali.security.policy import PolicyEngine
    from sali.tasks.watchdog import WatchdogConfig
    from sali.tools.registry import default_registry

    provider = FakeModelProvider(responses=[ChatResult("Okay, I've stopped that.", None, [], 2, 2, "fake")])
    loop = AgentLoop(
        pool=live_pool, provider=provider, retrieval=RetrievalService(live_pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())
    rt = AgentRuntime(loop, session_id=_uuid4(), pool=live_pool,
                      watchdog_config=WatchdogConfig(check_interval=timedelta(seconds=1)))
    store = loop._tasks
    task = await store.create("Build a big background thing", ["scaffold", "deploy"])
    await store.activate(task.id)

    await rt.handle_message("stop that", origin="cli")

    # The active task was durably revoked (tombstoned) — not left running to finish behind the cancel.
    assert await is_resumable(live_pool, task.id) is False
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM revoked_intent WHERE task_id=$1", task.id) == 1
        assert await c.fetchval("SELECT count(*) FROM task WHERE id=$1", task.id) == 0  # archived + removed
