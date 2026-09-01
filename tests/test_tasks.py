"""The Task Engine (§24): persistent multi-step tasks, their tools, and resume-across-restart."""

from __future__ import annotations

from pathlib import Path
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
    mid, _ = await store.advance(task.id, 2, "done")
    assert mid is not None and mid.status == "running"  # not all done yet

    end, _ = await store.advance(task.id, 3, "done")
    # Task auto-completes and is archived to sali-works/tasks/ + cleaned from DB
    assert end is None  # archived task returns None from get()
    assert not await store.open_tasks()  # no longer resurfaced


async def test_failed_step_records_attempts_and_failure_class(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Deploy", ["build", "start service"])
    # a failed step: the model's note carries the error; the store classifies + counts it (§8/§9)
    t, _ = await store.advance(task.id, 2, "failed", note="bind: permission denied on port 80")
    assert t is not None
    s2 = next(s for s in t.steps if s.seq == 2)
    assert s2.status == "failed" and s2.attempts == 1
    assert s2.failure_class == "permission" and "permission denied" in (s2.last_error or "")
    assert t.status == "running"  # a failed step is retryable, not a whole-task failure

    # retry + fail again: attempts increments, and an explicit error reclassifies (transient this time)
    t, _ = await store.advance(task.id, 2, "failed", error="connection timed out")
    assert t is not None
    s2 = next(s for s in t.steps if s.seq == 2)
    assert s2.attempts == 2 and s2.failure_class == "transient"

    # succeeding clears the failing path forward; attempts history is preserved on the row
    t, _ = await store.advance(task.id, 2, "done")
    assert t is not None
    s2 = next(s for s in t.steps if s.seq == 2)
    assert s2.status == "done" and s2.attempts == 2  # attempt count is not reset


async def test_done_step_links_verified_execution(live_pool: Any) -> None:
    from uuid import uuid4

    store = TaskStore(live_pool)
    task = await store.create("small", ["only step", "second step"])
    exec_id = uuid4()
    t, _ = await store.advance(task.id, 1, "done", verified_by=exec_id)
    assert t is not None  # task still running (step 2 pending)
    s1 = t.steps[0]
    assert s1.verified is True and s1.verified_by == exec_id  # "performed" now provably "verified" (§8)


async def test_advance_task_tool_auto_links_last_verified_execution(live_pool: Any) -> None:
    from uuid import uuid4

    store = TaskStore(live_pool)
    task = await store.create("deploy", ["configure", "start"])
    run_id = uuid4()
    exec_id = uuid4()
    # a prior tool in THIS run verified an effect (e.g. execute_command started the service)
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tool_execution "
            "  (id, run_id, tool_name, status, danger_level, plan, success, finished_at) "
            "VALUES ($1,$2,'execute_command','verified_success',1,$3,true, now())",
            exec_id, run_id, {"args": {}})

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, run_id=run_id, tasks=store)
    res = await AdvanceTask().run({"step": 1, "status": "done"}, ctx)
    assert res.ok
    got = await store.get(task.id)
    assert got is not None
    s1 = next(s for s in got.steps if s.seq == 1)
    assert s1.verified is True and s1.verified_by == exec_id  # tool auto-linked the verifying execution


async def test_next_step_respects_dependencies(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("pipeline", [
        "step one",
        {"description": "step two", "depends_on": [1]},
        {"description": "step three", "depends_on": [1, 2]},
    ])
    assert task.next_step is not None and task.next_step.seq == 1  # only step 1 is ready
    assert [s.seq for s in task.blocked_steps] == [2, 3]

    t, _ = await store.advance(task.id, 1, "done")
    assert t is not None and t.next_step is not None and t.next_step.seq == 2  # 2 ready, 3 still blocked
    assert [s.seq for s in t.blocked_steps] == [3]

    t, _ = await store.advance(task.id, 2, "done")
    assert t is not None and t.next_step is not None and t.next_step.seq == 3  # deps satisfied → 3 ready
    assert t.blocked_steps == []


async def test_step_checkpoint_resumes_mid_step(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("bulk job", ["download 100 files"])
    await store.checkpoint(task.id, 1, {"downloaded": 42, "of": 100})
    # a fresh read (simulated restart) sees the in-step progress and marks the step running
    resumed = await store.get(task.id)
    assert resumed is not None
    s1 = resumed.steps[0]
    assert s1.status == "running" and s1.checkpoint == {"downloaded": 42, "of": 100}
    line = resumed.one_line()
    assert "resuming mid-step" in line and "downloaded=42" in line  # jsonb key order isn't preserved


async def test_finish_task_closes_it(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Big thing", ["a", "b"])
    await store.finish(task.id, status="abandoned", result="not worth it")
    # Task is archived to sali-works/tasks/ and cleaned from DB
    got = await store.get(task.id)
    assert got is None  # archived and deleted
    assert not await store.open_tasks()


# ---- the tools, over a fake sink ---------------------------------------------------------------
class _FakeTasks:
    def __init__(self) -> None:
        self.created: list[tuple[str, list[str]]] = []
        self.advanced: list[tuple[int, str]] = []
        self.finished: list[str] = []
        self.checkpoints: list[tuple[int, dict[str, Any]]] = []
        self._current: Any = None

    async def create(self, objective: str, steps: list[str], **kwargs: Any) -> Any:
        self.created.append((objective, steps))
        self._current = SimpleNamespace(id=new_id(), objective=objective, status="open", steps=steps)
        return self._current

    async def current(self) -> Any:
        return self._current

    async def advance(
        self, task_id: Any, step_seq: int, status: str, *, note: str | None = None,
        error: str | None = None, verified_by: Any = None, run_id: Any = None,
    ) -> tuple[Any, str | None]:
        self.advanced.append((step_seq, status))
        ns = SimpleNamespace(id=task_id, objective="x", next_step=None,
                             status="done" if status == "done" else "running")
        return (ns, None)

    async def checkpoint(self, task_id: Any, step_seq: int, data: dict[str, Any]) -> None:
        self.checkpoints.append((step_seq, data))

    async def finish(
        self, task_id: Any, *, status: str = "done", result: str | None = None, run_id: Any = None,
    ) -> str | None:
        self.finished.append(status)
        return None


def _ctx(tasks: _FakeTasks | None) -> ToolContext:
    # _FakeTasks is a duck-typed stand-in for the TaskSink protocol (attributes only).
    return ToolContext(settings=Settings(), clock=SystemClock(), tasks=tasks)  # type: ignore[arg-type]


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


# ---- confirm_task and modify_task tools --------------------------------------------------------

async def test_confirm_task_deletes_folder(live_pool: Any, tmp_path: Path) -> None:
    """confirm_task deletes the task folder from sali-works/tasks/."""
    from unittest.mock import patch

    from sali.tools.builtins.task_tool import ConfirmTask

    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create files"])
    # Create the task folder
    task_dir = tmp_path / str(task.id)
    task_dir.mkdir(parents=True)
    (task_dir / "meta.json").write_text('{"test": true}')

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, tasks=store)
    with patch("sali.tasks.logger._tasks_base", return_value=tmp_path):
        result = await ConfirmTask().run({"task_id": str(task.id), "satisfaction": "looks good"}, ctx)
    assert result.ok
    assert not task_dir.exists()  # folder deleted


async def test_modify_task_add_step(live_pool: Any) -> None:
    """modify_task can add a new step."""
    from sali.tools.builtins.task_tool import ModifyTask

    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS"])
    await store.activate(task.id)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, tasks=store)
    result = await ModifyTask().run(
        {"action": "add", "description": "add JavaScript"}, ctx)
    assert result.ok
    # Verify the step was added
    got = await store.get(task.id)
    assert got is not None
    assert len(got.steps) == 3
    assert got.steps[2].description == "add JavaScript"


async def test_modify_task_replace_step(live_pool: Any) -> None:
    """modify_task can replace a step's description."""
    from sali.tools.builtins.task_tool import ModifyTask

    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS"])
    await store.activate(task.id)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, tasks=store)
    result = await ModifyTask().run(
        {"action": "replace", "step": 2, "description": "add Tailwind CSS"}, ctx)
    assert result.ok
    got = await store.get(task.id)
    assert got is not None
    assert got.steps[1].description == "add Tailwind CSS"


async def test_modify_task_remove_step(live_pool: Any) -> None:
    """modify_task can remove a step."""
    from sali.tools.builtins.task_tool import ModifyTask

    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS", "add JS"])
    await store.activate(task.id)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, tasks=store)
    result = await ModifyTask().run({"action": "remove", "step": 2}, ctx)
    assert result.ok
    got = await store.get(task.id)
    assert got is not None
    assert len(got.steps) == 2
    assert got.steps[0].description == "create index.html"
    assert got.steps[1].description == "add JS"


async def test_modify_task_reset_step(live_pool: Any) -> None:
    """modify_task can reset a step to pending."""
    from sali.tools.builtins.task_tool import ModifyTask

    store = TaskStore(live_pool)
    task = await store.create("Build site", ["create index.html", "add CSS"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool, tasks=store)
    result = await ModifyTask().run({"action": "reset", "step": 1}, ctx)
    assert result.ok
    got = await store.get(task.id)
    assert got is not None
    assert got.steps[0].status == "pending"


# ---- Prompt 1: suspension / resume lifecycle ----------------------------------------------------
async def test_suspend_keeps_primary_and_resume_reactivates(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Build website", ["scaffold", "auth", "deploy"])
    await store.activate(task.id)
    suspended = await store.suspend(task.id, reason="check nginx")
    assert suspended is not None and suspended.status == "paused"
    # still THE primary (is_primary preserved) and still the active task — it survives the interrupt
    active = await store.active_task()
    assert active is not None and active.id == task.id and active.status == "paused"
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT is_primary, interrupted_at FROM task WHERE id=$1", task.id)
    assert row["is_primary"] is True and row["interrupted_at"] is not None
    # resume → running again, the SAME task (never a new project)
    resumed = await store.resume(task.id)
    assert resumed is not None and resumed.id == task.id and resumed.status == "running"


async def test_cancelling_an_interrupt_does_not_touch_the_primary(live_pool: Any) -> None:
    # §13: the primary is independent of a quick interrupt's own message/run. Cancelling the interrupt
    # leaves the suspended primary intact and resumable — it is NOT destroyed.
    from sali.tasks.inbox import MessageInbox

    store = TaskStore(live_pool)
    task = await store.create("Build website", ["a", "b"])
    await store.activate(task.id)
    await store.suspend(task.id, reason="zip a file")
    inbox = MessageInbox(live_pool)
    interrupt = await inbox.enqueue("zip project.zip", related_task_id=task.id)
    assert interrupt is not None
    await inbox.cancel(interrupt.id)  # user cancels the quick action
    active = await store.active_task()
    assert active is not None and active.id == task.id and active.status == "paused"  # primary survives
    resumed = await store.resume(task.id)
    assert resumed is not None and resumed.status == "running"
