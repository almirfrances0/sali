"""Long-running task continuity through the LIVE loop + runtime (Prompt 3).

The guarantee under test, end to end: when the context window fills, Sali compacts and continues
WITHOUT losing task state; a provider context-overflow is recovered a bounded number of times and
never fails the task; recovery is deterministic from PostgreSQL; and ONE logical task (a stable
task_id) survives many folds, an interruption, several runs, and a simulated process restart.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.errors import ProviderError
from sali.events.publisher import EventPublisher
from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import _MAX_OVERFLOW_RECOVERIES, AgentLoop
from sali.runtime.runtime import AgentRuntime
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.watchdog import TaskWatchdog, WatchdogConfig
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())
    loop._publisher = EventPublisher(pool)          # so runtime.* events land durably in `event`
    loop._tasks._publisher = loop._publisher
    return loop


def _runtime(pool: Any, provider: FakeModelProvider) -> AgentRuntime:
    loop = AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())
    return AgentRuntime(loop, session_id=uuid4(), pool=pool,
                        watchdog_config=WatchdogConfig(check_interval=timedelta(seconds=1)))


async def _clear_lease(pool: Any) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE execution_lease SET status='releasing' WHERE id='foreground'")


@pytest.fixture(autouse=True)
async def _reset_lease(live_pool: Any) -> Any:
    # execution_lease is a singleton (not in the truncate set); reset it around each test so the
    # foreground lease never leaks between tests (only the integration test uses the coordinator).
    await _clear_lease(live_pool)
    yield
    await _clear_lease(live_pool)


class _OverflowStream(FakeModelProvider):
    """chat_stream raises a context-overflow ProviderError `fail_times` times, then streams `final`.
    chat() (the fold summary / stall judge) always returns a normal note, so a fold can proceed even
    while streaming keeps rejecting the oversized request."""

    def __init__(self, *, fail_times: int, final: str = "Continued after compaction.") -> None:
        super().__init__()
        self._fail_times = fail_times
        self._final = final
        self.stream_calls = 0

    async def chat(self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
                   options: dict[str, Any] | None = None, think: bool = False) -> ChatResult:
        return ChatResult("DONE: folded my work; NEXT: keep building.", None, [], 3, 3, "fake")

    async def chat_stream(self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
                          options: dict[str, Any] | None = None,
                          think: bool = False) -> AsyncIterator[ChatChunk]:
        self.stream_calls += 1
        if self.stream_calls <= self._fail_times:
            raise ProviderError(
                "ollama chat stream failed: 500 - context length exceeded (num_ctx too small)")
        yield ChatChunk(content=self._final + " ")
        yield ChatChunk(done=True, result=ChatResult(self._final, None, [], 3, 3, "fake"))


class _FoldingStream(FakeModelProvider):
    """Drives `tool_iters` tool-calling cycles (so the working prompt grows and folds repeatedly at the
    top of each cycle under a tiny window), then a final. chat() is auxiliary-only (fold summary/stall),
    so it never consumes the streaming script."""

    def __init__(self, *, tool_iters: int, ctx_limit: int) -> None:
        super().__init__(ctx_limit=ctx_limit)
        self._left = tool_iters

    async def chat(self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
                   options: dict[str, Any] | None = None, think: bool = False) -> ChatResult:
        return ChatResult("DONE: progress folded; NEXT: continue.", None, [], 3, 3, "fake")

    async def chat_stream(self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
                          options: dict[str, Any] | None = None,
                          think: bool = False) -> AsyncIterator[ChatChunk]:
        if self._left > 0:
            self._left -= 1
            res = ChatResult("", None, [ToolCall("memory_info", {})], 5, 5, "fake")
        else:
            res = ChatResult("All done — the build is complete.", None, [], 5, 5, "fake")
        yield ChatChunk(done=True, result=res)


# ── emergency context overflow: compact and continue, never lose the task (§8/§29) ─────────────────

async def test_context_overflow_recovers_and_keeps_the_same_task(live_pool: Any) -> None:
    prov = _OverflowStream(fail_times=1)
    loop = _loop(live_pool, prov)
    store = loop._tasks
    task = await store.create("Build the app", ["scaffold", "auth"])
    await store.activate(task.id)

    result = await loop.run("keep going on the app build")

    assert result.text.startswith("Continued")   # it recovered and produced a real answer
    assert prov.stream_calls == 2                 # rejected once, succeeded after compacting
    active = await store.active_task()
    assert active is not None and active.id == task.id   # SAME task — overflow never forks a task
    async with live_pool.acquire() as c:
        recovered = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='runtime.context_overflow_recovered'")
        n_tasks = await c.fetchval("SELECT count(*) FROM task")
    assert recovered >= 1
    assert n_tasks == 1                           # no new task was created (§30.17)


async def test_context_overflow_retry_is_bounded_and_preserves_the_task(live_pool: Any) -> None:
    prov = _OverflowStream(fail_times=99)         # the request never fits
    loop = _loop(live_pool, prov)
    store = loop._tasks
    task = await store.create("Build the app", ["scaffold"])
    await store.activate(task.id)

    with pytest.raises(ProviderError):
        await loop.run("keep going on the app build")

    # bounded — never an infinite loop: N overflow recoveries, then ONE emergency-continuation attempt
    # (Prompt 6 §42) with the minimal capsule, then it surfaces.
    assert prov.stream_calls == _MAX_OVERFLOW_RECOVERIES + 2
    active = await store.active_task()
    assert active is not None and active.id == task.id         # the task is intact and still resumable
    async with live_pool.acquire() as c:
        failed = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='runtime.context_compaction_failed'")
    assert failed >= 1


# ── proactive multi-compaction in a single turn, with observable events (§7/§22/§37) ───────────────

async def test_many_compactions_in_one_turn_emit_identified_events(live_pool: Any) -> None:
    prov = _FoldingStream(tool_iters=3, ctx_limit=400)   # tiny window → fold every cycle
    loop = _loop(live_pool, prov)
    store = loop._tasks
    task = await store.create("Build the app", ["a", "b"])
    await store.activate(task.id)

    result = await loop.run("keep going on the app build")
    assert "done" in result.text.lower()

    async with live_pool.acquire() as c:
        rows = await c.fetch(
            "SELECT payload FROM event WHERE event_type='runtime.context_compaction_completed' "
            "ORDER BY seq")
    assert len(rows) >= 2                          # folded more than once in ONE turn (§30.37)
    first = rows[0]["payload"]
    # every compaction event carries full identity so the iPhone can follow it (§22/§30.36)
    assert first.get("run_id") and first.get("task_id") and first.get("session_id")
    assert first.get("count") == 1 and rows[1]["payload"].get("count") == 2


async def test_compaction_records_progress_so_watchdog_sees_work(live_pool: Any) -> None:
    prov = _FoldingStream(tool_iters=2, ctx_limit=400)
    loop = _loop(live_pool, prov)
    store = loop._tasks
    task = await store.create("Build the app", ["a", "b"])
    await store.activate(task.id)

    await loop.run("keep going on the app build")

    t = await store.get(task.id)
    assert t is not None and t.health_status == "healthy"
    assert t.last_progress_type in ("compaction", "tool_success", "step_advance")
    # the watchdog classifier reads a compacting task as healthy work, never stuck/orphaned (§21)
    wd = TaskWatchdog(live_pool, WatchdogConfig())
    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT id, objective, status, last_heartbeat, last_progress_at, last_progress_type, "
            "  active_tool_name, health_status, updated_at FROM task WHERE id=$1", task.id)
    assert wd._classify(row) in ("healthy", "active_tool")


async def test_task_authority_still_runs_on_a_compacted_turn(live_pool: Any) -> None:
    prov = _OverflowStream(fail_times=1)
    loop = _loop(live_pool, prov)
    store = loop._tasks
    task = await store.create("Build the app", ["a"])
    await store.activate(task.id)

    result = await loop.run("keep going on the app build")

    async with live_pool.acquire() as c:
        authority = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='task_authority'", result.run_id)
    assert authority == 1     # even a turn that compacted still passed through TaskAuthority (§19)


# ── the capstone: one task survives compaction + interrupt + several runs + restart (§31) ──────────

async def test_end_to_end_long_task_survives_compaction_interrupt_and_restart(live_pool: Any) -> None:
    prov = _FoldingStream(tool_iters=1, ctx_limit=100_000)   # normal window; no incidental folds
    rt = _runtime(live_pool, prov)
    loop = rt._loop
    store = loop._tasks
    task = await store.create("Build the Laravel app", ["scaffold", "auth", "migrate", "deploy"])
    await store.activate(task.id)
    original_id = task.id

    # Durable progress: step 1 has a completed execution and is verified-done; step 2 has a mid-step
    # checkpoint; an artifact exists. This is the state that must survive EVERYTHING below.
    ex = await store.record_execution(task.id, 1, "create_file", tool_args={"path": "composer.json"})
    await store.complete_execution(ex, status="completed", result_summary="scaffolded")
    _, err = await store.advance(task.id, 1, "done", verified_by=ex)
    assert err is None
    await store.checkpoint(task.id, 2, {"progress": "auth wired 50%"})
    await store.record_artifact(task.id, "/w/composer.json", "created", tool_name="create_file")

    # 1) COMPACT repeatedly — the deterministic task header is re-read from the STORE each fold, so the
    #    objective and the NEXT step survive no matter how many times we fold (nothing is lost).
    msgs = [ChatMessage(role="system", content="S"),
            ChatMessage(role="user", content="Build the Laravel app")]
    msgs += [ChatMessage(role="assistant", content="work " * 300) for _ in range(8)]
    for _ in range(4):
        folded = await loop._fold_messages(msgs)
        assert len(folded) <= 4
        carried = "\n".join(m.content or "" for m in folded)
        assert "Build the Laravel app" in carried        # objective survives every fold
        assert "auth" in carried                          # the NEXT ready step (2) survives
        msgs = folded + [ChatMessage(role="assistant", content="more " * 300) for _ in range(8)]

    # 2) INTERRUPT → suspend → automatic resume (the Prompt 2 reflex) — SAME task, no corruption (§20)
    await store.suspend(task.id, reason="quick question")
    resumed = await rt._maybe_resume_primary(await store.get(task.id), None, None)
    assert resumed["resumed"] is True
    active = await store.active_task()
    assert active is not None and active.id == original_id and active.status == "running"

    # 3) two more real runs — a NEW run_id each, all under the SAME task_id (task ≠ run)
    r1 = await loop.run("keep going on the Laravel app build")
    r2 = await loop.run("continue the Laravel app build")
    assert r1.run_id != r2.run_id
    still = await store.active_task()
    assert still is not None and still.id == original_id

    # 4) SIMULATED RESTART: the heartbeat goes stale → deterministic, DB-driven recovery; task_id stable
    async with live_pool.acquire() as c:
        await c.execute(
            "UPDATE task SET last_heartbeat = now() - interval '10 minutes' WHERE id=$1", task.id)
    recovered = await loop.recover_tasks()
    rec = next((r for r in recovered if r["task_id"] == str(original_id)), None)
    assert rec is not None and rec["completed_steps"] >= 1   # step 1 stayed done — recovery didn't lose it

    # 5) every durable fact survived compaction, interruption, several runs, and the restart
    assert await store.has_completed_execution(task.id, 1, "create_file")   # not duplicated, not lost
    t = await store.get(task.id)
    assert t is not None and t.id == original_id
    assert t.steps[0].status == "done" and t.steps[0].verified          # verified evidence intact
    assert t.steps[1].checkpoint == {"progress": "auth wired 50%"}       # mid-step checkpoint intact
    arts = await store.artifacts(task.id)
    assert any(a["artifact_path"] == "/w/composer.json" for a in arts)   # artifact intact
