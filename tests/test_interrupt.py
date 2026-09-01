"""Live interruptible attention (Prompt 2): the end-to-end reflex — a message arriving mid-run yields the
current RUN, suspends the primary TASK, runs the interrupt as its own run, then AUTOMATICALLY resumes the
primary from durable state. Plus the invariants: distinct run_ids, don't-resume rules, idempotency, queue,
cross-process preemption, and one-foreground-at-a-time."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolSpec
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.lease import ExecutionLease
from sali.runtime.loop import AgentLoop
from sali.runtime.runtime import AgentRuntime
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.watchdog import WatchdogConfig
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


class _SlowThenScripted(FakeModelProvider):
    """First streamed turn is slow + interruptible (yields with sleeps so the coordinator can cancel it at
    a safe boundary); later turns use the scripted responses (the interrupt run, then the resume run)."""

    def __init__(self, *, scripted: list[ChatResult]) -> None:
        super().__init__(responses=scripted)
        self.running = asyncio.Event()
        self._slow_served = False

    async def chat_stream(
        self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None, think: bool = False,
    ) -> AsyncIterator[ChatChunk]:
        if not self._slow_served:
            self._slow_served = True
            self.running.set()
            for _ in range(300):  # ~6s ceiling; the interrupt cancels it far sooner
                yield ChatChunk(content="working ")
                await asyncio.sleep(0.02)  # a boundary for the coordinator to observe cancellation
            yield ChatChunk(done=True, result=ChatResult("primary partial", None, [], 3, 3, "fake"))
            return
        async for chunk in super().chat_stream(messages, tools=tools, options=options, think=think):
            yield chunk


def _runtime(pool: Any, provider: FakeModelProvider) -> AgentRuntime:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))
    return AgentRuntime(loop, session_id=uuid4(), pool=pool,
                        watchdog_config=WatchdogConfig(check_interval=timedelta(seconds=1)))


async def _clear_lease(pool: Any) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE execution_lease SET status='releasing' WHERE id='foreground'")


@pytest.fixture(autouse=True)
async def _reset_lease(live_pool: Any) -> Any:
    # execution_lease is a singleton (not in the truncate set), so reset it around each test to keep the
    # foreground lease from leaking between tests (and into test_lease).
    await _clear_lease(live_pool)
    yield
    await _clear_lease(live_pool)


async def test_interrupt_suspends_and_auto_resumes(live_pool: Any) -> None:
    await _clear_lease(live_pool)
    slow = _SlowThenScripted(scripted=[
        ChatResult("Zipped report.zip and sent it. Done.", None, [], 3, 3, "fake"),  # R2 interrupt
        ChatResult("Resumed — continuing the build.", None, [], 3, 3, "fake"),       # R3 resume
    ])
    rt = _runtime(live_pool, slow)
    store = rt._loop._tasks
    task = await store.create("Build the app", ["scaffold", "auth", "deploy"])
    await store.activate(task.id)

    # R1: the primary run starts and streams slowly (busy)
    r1_fut = asyncio.create_task(rt.handle_message("keep building the app", origin="cli"))
    await asyncio.wait_for(slow.running.wait(), timeout=5)
    assert rt._coordinator.is_busy  # foreground is occupied

    # An interrupt arrives (in-process): yield the run, suspend the task, do it, auto-resume
    r2 = await asyncio.wait_for(
        rt.handle_message("stop for a moment and zip report.zip and send it", origin="ios"), timeout=20)
    assert r2["category"] == "interrupt_task"
    assert r2["result"]["status"] in ("completed", "cancelled")
    assert r2["resume"]["resumed"] is True  # the primary was automatically resumed

    # primary is running again — the SAME task_id (never a new project)
    active = await store.active_task()
    assert active is not None and active.id == task.id and active.status == "running"

    r1 = await asyncio.wait_for(r1_fut, timeout=5)
    assert r1["result"]["status"] == "cancelled"  # the primary RUN was yielded, not the task

    # distinct run identities: R1 (primary) ≠ R2 (interrupt) ≠ R3 (resume)
    runs = {r1["result"]["run_id"], r2["result"]["run_id"], r2["resume"]["run_id"]}
    assert len(runs) == 3


async def test_maybe_resume_respects_dont_resume_rules(live_pool: Any) -> None:
    await _clear_lease(live_pool)
    rt = _runtime(live_pool, FakeModelProvider())
    store = rt._loop._tasks

    # cancelled primary → never auto-resumes (§9)
    t1 = await store.create("job one", ["a"])
    await store.activate(t1.id)
    await store.suspend(t1.id, reason="x")
    await store.cancel(t1.id, reason="user stopped it")
    assert (await rt._maybe_resume_primary(t1, None, None))["resumed"] is False

    # superseded / no-longer-primary → never auto-resumes
    t2 = await store.create("job two", ["a"])
    await store.activate(t2.id)
    await store.suspend(t2.id, reason="x")
    t3 = await store.create("job three", ["a"])
    await store.supersede(t2.id, t3.id, reason="replaced")  # t2 no longer primary
    assert (await rt._maybe_resume_primary(t2, None, None))["resumed"] is False

    # a running (not paused) task is not a resume candidate
    t4 = await store.create("job four", ["a"])
    await store.activate(t4.id)  # status running, not paused
    assert (await rt._maybe_resume_primary(t4, None, None))["resumed"] is False


async def test_duplicate_message_is_not_executed_twice(live_pool: Any) -> None:
    await _clear_lease(live_pool)
    rt = _runtime(live_pool, FakeModelProvider())
    await rt.route_incoming("send the report", dedup_key="dedupe-1")  # pre-queued
    d = await rt.handle_message("send the report", dedup_key="dedupe-1")
    assert d["status"] == "duplicate"  # the reflex refuses to run it again (§15)


async def test_queue_for_later_is_not_executed(live_pool: Any) -> None:
    await _clear_lease(live_pool)
    rt = _runtime(live_pool, FakeModelProvider())
    d = await rt.handle_message("after you finish, remind me to deploy it")
    assert d["status"] == "queued"  # persisted, not run now (§10)
    assert any(m.content.startswith("after you finish") for m in await rt.inbox.pending())


async def test_cross_process_preemption_protocol(live_pool: Any) -> None:
    await _clear_lease(live_pool)
    la = ExecutionLease(live_pool)
    la._owner_id = "A@test"
    lb = ExecutionLease(live_pool)
    lb._owner_id = "B@test"
    assert await la.try_acquire(uuid4(), uuid4(), "cli") is True
    assert await la.preempt_requested() is False
    # another process (owner B) asks A to yield the foreground
    assert await lb.request_preemption("iphone interrupt") is True
    assert await la.preempt_requested() is True  # A's poll would see this and cancel its run
    await la.release()
    assert await lb.try_acquire(uuid4(), uuid4(), "api") is True  # B takes over → clears the request
    assert await lb.preempt_requested() is False


async def test_only_one_foreground_lease_at_a_time(live_pool: Any) -> None:
    await _clear_lease(live_pool)
    la = ExecutionLease(live_pool)
    la._owner_id = "A@test"
    lb = ExecutionLease(live_pool)
    lb._owner_id = "B@test"
    assert await la.try_acquire(uuid4(), uuid4(), "cli") is True
    assert await lb.try_acquire(uuid4(), uuid4(), "api") is False  # global single-foreground invariant
    assert await la.is_available() is False
