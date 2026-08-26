"""The Task Engine (§24): persistent multi-step tasks, their tools, and resume-across-restart."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.clock import SystemClock
from sali.core.ids import new_id
from sali.provider.base import ChatResult, ToolCall
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.store import TaskStore
from sali.tools.builtins.task_tool import AdvanceTask, FinishTask, PlanTask
from sali.tools.context import ToolContext
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


async def test_task_lifecycle_persists_and_autocompletes(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Investigate VPS", ["check cpu", "check disk", "recommend"])
    assert task.status == "open" and len(task.steps) == 3
    assert task.next_step is not None and task.next_step.seq == 1

    assert any(t.id == task.id for t in await store.open_tasks())  # resumable

    await store.advance(task.id, 1, "done", note="cpu fine")
    mid = await store.advance(task.id, 2, "done")
    assert mid is not None and mid.status == "running"  # not all done yet

    end = await store.advance(task.id, 3, "done")
    assert end is not None and end.status == "done"  # every step done → task done
    assert not await store.open_tasks()  # no longer resurfaced


async def test_finish_task_closes_it(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Big thing", ["a", "b"])
    await store.finish(task.id, status="abandoned", result="not worth it")
    got = await store.get(task.id)
    assert got is not None and got.status == "abandoned" and got.result == "not worth it"
    assert not await store.open_tasks()


# ---- the tools, over a fake sink ---------------------------------------------------------------
class _FakeTasks:
    def __init__(self) -> None:
        self.created: list[tuple[str, list[str]]] = []
        self.advanced: list[tuple[int, str]] = []
        self.finished: list[str] = []
        self._current: Any = None

    async def create(self, objective: str, steps: list[str]) -> Any:
        self.created.append((objective, steps))
        self._current = SimpleNamespace(id=new_id(), objective=objective, status="open", steps=steps)
        return self._current

    async def current(self) -> Any:
        return self._current

    async def advance(self, task_id: Any, step_seq: int, status: str, *, note: str | None = None) -> Any:
        self.advanced.append((step_seq, status))
        return SimpleNamespace(id=task_id, objective="x", status="done" if status == "done" else "running")

    async def finish(self, task_id: Any, *, status: str = "done", result: str | None = None) -> None:
        self.finished.append(status)
        self._current = None


def _ctx(tasks: _FakeTasks | None) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), tasks=tasks)


async def test_task_tools_plan_advance_finish() -> None:
    tasks = _FakeTasks()
    ctx = _ctx(tasks)

    planned = await PlanTask().run(
        {"objective": "Deploy site", "steps": ["build", "upload", "verify"]}, ctx)
    assert planned.ok and tasks.created == [("Deploy site", ["build", "upload", "verify"])]

    adv = await AdvanceTask().run({"step": 1, "status": "done"}, ctx)
    assert adv.ok and tasks.advanced == [(1, "done")]

    fin = await FinishTask().run({"status": "done", "result": "shipped"}, ctx)
    assert fin.ok and tasks.finished == ["done"]


async def test_task_tools_guard_missing_engine_and_no_open_task() -> None:
    no_engine = await PlanTask().run({"objective": "x", "steps": ["a"]}, _ctx(None))
    assert not no_engine.ok and "task engine" in (no_engine.error or "")

    no_task = await AdvanceTask().run({"step": 1, "status": "done"}, _ctx(_FakeTasks()))
    assert not no_task.ok and "no open task" in (no_task.error or "")


# ---- resume across a "restart" -----------------------------------------------------------------
def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool, provider=provider, retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))


async def test_task_survives_restart_and_resurfaces_in_context(live_pool: Any) -> None:
    # Turn 1: the model plans a task (persisted). Then a BRAND-NEW loop (a simulated restart) reads
    # the still-open task back out of the datastore into its context — that is §24 "survive restart".
    fake = FakeModelProvider(responses=[
        ChatResult("", None, [ToolCall("plan_task", {
            "objective": "Deploy the site", "steps": ["build", "upload", "verify"]})], 4, 2, "fake"),
        ChatResult("Planned it — I'll start on the build.", None, [], 4, 3, "fake"),
        ChatResult("DONE", None, [], 1, 1, "fake"),
    ])
    await _loop(live_pool, fake).run("deploy the site, it's a few steps", session_id=new_id())

    restarted = _loop(live_pool, FakeModelProvider())  # fresh process, nothing in memory
    note = await restarted._open_tasks_note()
    assert note is not None and "Deploy the site" in note and "next: step 1" in note
