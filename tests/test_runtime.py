"""AgentRuntime — the single execution authority: publisher wiring, background/foreground submit
(serialized via the PostgreSQL lease), recover, and start/aclose lifecycle."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.ids import new_id
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.coordinator import ExecutionOrigin
from sali.runtime.loop import AgentLoop
from sali.runtime.runtime import AgentRuntime
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.watchdog import WatchdogConfig
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))


def _runtime(pool: Any, provider: FakeModelProvider) -> AgentRuntime:
    return AgentRuntime(
        _loop(pool, provider), session_id=new_id(), pool=pool,
        watchdog_config=WatchdogConfig(check_interval=timedelta(seconds=1)))


async def test_runtime_wires_one_publisher_and_exposes_state(live_pool: Any) -> None:
    rt = _runtime(live_pool, FakeModelProvider())
    # the canonical publisher is threaded into the loop AND its task store — one event path
    assert rt.loop._publisher is rt._publisher
    assert rt.loop._tasks._publisher is rt._publisher
    assert rt.coordinator is not None and rt.lease is not None
    assert rt.is_busy is False and rt.current_execution is None
    assert rt.session_id == rt._session_id


async def test_submit_background_runs_a_turn(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[ChatResult("hello from background", None, [], 3, 3, "fake")])
    rt = _runtime(live_pool, fake)
    result = await rt.submit_background("say hi", session_id=new_id())
    assert result["status"] == "completed"
    assert "hello from background" in result["text"]
    assert result["run_id"]


async def test_submit_foreground_serializes_and_releases_the_lease(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[ChatResult("done in foreground", None, [], 3, 3, "fake")])
    rt = _runtime(live_pool, fake)
    await rt.recover()  # clear any stale lease left by a prior test
    result = await rt.submit_foreground("do a thing", ExecutionOrigin.CLI, timeout=30.0)
    assert result.get("status")  # coordinator returns a result dict
    assert await rt.lease.is_available() is True  # lease released after the turn


async def test_recover_and_lifecycle(live_pool: Any) -> None:
    rt = _runtime(live_pool, FakeModelProvider())
    await rt.start()  # watchdog up
    assert isinstance(await rt.recover(), list)  # lease.recover_stale + loop.recover
    assert isinstance(await rt.recover_tasks(), list)
    await rt.aclose()  # watchdog stop + lease release + loop aclose — no error


async def test_route_incoming_classifies_persists_and_recovers(live_pool: Any) -> None:
    from sali.tasks.store import TaskStore

    rt = _runtime(live_pool, FakeModelProvider())
    # no primary yet → a substantive request becomes the focus
    d = await rt.route_incoming("build me a flask api", origin="ios")
    assert d["category"] == "continue_primary" and d["message_id"]

    store = TaskStore(live_pool)
    task = await store.create("Build the app", ["a", "b"])
    await store.activate(task.id)
    # a status question must NOT touch the primary task
    d = await rt.route_incoming("what are you doing?")
    assert d["category"] == "conversation" and not d["touches_primary"] and d["has_primary"]

    # suspend for an interrupt, then recovery reports it + resume brings it back
    await rt.suspend_primary(reason="check nginx")
    snap = await rt.recover_attention()
    assert snap["primary_suspended"] and snap["should_resume_primary"]
    resumed = await rt.resume_primary()
    assert resumed is not None and resumed.status == "running"

    # the routed messages are durably queued (survive restart)
    assert any(m.content == "what are you doing?" for m in await rt.inbox.pending())
