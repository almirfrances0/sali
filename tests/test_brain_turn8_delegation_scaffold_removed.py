"""Brain-audit Turn 8: inert delegation scaffold removed.

Sali is a single executive agent (Prompt 12 §7 + user directive) — no subagent runtime, no second
reasoning stream. Pre-Turn-8 the codebase carried a full inert scaffold that proved this by REFUSING:
- `Delegate` tool class (unregistered, always refused if invoked)
- `_DelegateSink` runtime sink (held DelegationStore it never wrote to)
- `DelegationStore` (with a 0-or-1 partial-unique DB index that protected nothing since nothing wrote)
- `ToolContext.delegate` channel (passed to every tool but read by nothing)
- ~60 lines of tests exercising the refuse-branch

Turn 8 deleted all of it. The invariant "no second model call" is now enforced by ABSENCE — there
is no code path to try — instead of by a class that runs at every ctx construction and always says no.

Source-level guards + AST guards so the scaffold can't quietly return.
"""

from __future__ import annotations

import ast
import pathlib


def _read(rel: str) -> str:
    return pathlib.Path(rel).read_text()


def _module_top_level_names(src: str) -> set[str]:
    """All top-level class/function names in a module."""
    tree = ast.parse(src)
    return {n.name for n in tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


def test_delegate_tool_class_is_gone() -> None:
    src = _read("src/sali/tools/builtins/agent_tool.py")
    names = _module_top_level_names(src)
    assert "Delegate" not in names, (
        "Turn 8 regressed: `Delegate` tool class is back in agent_tool.py. Sali is a single "
        "executive agent — no delegation tool should exist, even an unregistered one that "
        "refuses. Absence is the invariant.")


def test_delegate_sink_is_gone() -> None:
    src = _read("src/sali/runtime/loop.py")
    names = _module_top_level_names(src)
    assert "_DelegateSink" not in names, (
        "Turn 8 regressed: `_DelegateSink` class is back in loop.py. The sink was inert — held a "
        "DelegationStore it never wrote to. No delegation runtime should exist.")


def test_delegation_store_is_gone() -> None:
    src = _read("src/sali/tasks/coordination.py")
    names = _module_top_level_names(src)
    assert "DelegationStore" not in names, (
        "Turn 8 regressed: `DelegationStore` class is back in coordination.py. The store's "
        "0-or-1 partial-unique DB index protected nothing because the sink never called start(). "
        "QuestionStore should be the only class in this module.")
    assert "QuestionStore" in names, "QuestionStore must remain (user-clarification, still live)."


def test_tool_context_has_no_delegate_channel() -> None:
    """AST scan: no top-level annotated assignment (dataclass field) named `delegate` on ToolContext."""
    src = _read("src/sali/tools/context.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolContext":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    assert item.target.id != "delegate", (
                        "Turn 8 regressed: `ToolContext.delegate` field is back. This channel "
                        "existed only to pass an inert _DelegateSink to every tool.")
            return
    raise AssertionError("ToolContext class not found in tools/context.py")


def test_loop_does_not_import_delegation_store() -> None:
    """AST scan: loop.py must not import DelegationStore."""
    src = _read("src/sali/runtime/loop.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "DelegationStore", (
                    "Turn 8 regressed: loop.py imports DelegationStore again. It should import "
                    "QuestionStore only from sali.tasks.coordination.")


def test_agent_tool_module_only_exports_askuser() -> None:
    """agent_tool.py should carry ONLY the AskUser class after Turn 8."""
    src = _read("src/sali/tools/builtins/agent_tool.py")
    tree = ast.parse(src)
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    assert classes == ["AskUser"], (
        f"Turn 8 regressed: agent_tool.py should carry ONLY the AskUser class. Got: {classes}")


def test_delegate_string_absent_from_prompt_advertisements() -> None:
    """No 'delegate' tool name string in the tool registry advertisement path (registry.register call)."""
    src = _read("src/sali/tools/builtins/agent_tool.py")
    # A `registry.register(...)` call whose argument constructs a class named Delegate would
    # reintroduce the tool. Reject any such call.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "register":
                for arg in node.args:
                    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                        assert arg.func.id != "Delegate", (
                            "Turn 8 regressed: register_builtins registers a Delegate tool.")


# ── Fold-in from adversarial review (test-coverage lens): the deleted refuse-branch tests
# ── silently dropped coverage of the `is_subagent=True` guards on 6 tools. Restore that coverage
# ── as a standalone test — a single-agent invariant that only lived inside the delegation tests.

import pytest
from uuid import uuid4


@pytest.mark.asyncio
async def test_all_task_tools_refuse_subagent_context() -> None:
    """The 6 refuse branches for `ctx.is_subagent=True` — one on each task-managing tool + AskUser
    — must all reject cleanly. Turn 8 deleted the delegation tests that used to exercise this
    (via `sub = ToolContext(..., is_subagent=True)`). Adversarial review flagged the coverage
    loss: if any of the 6 `if ctx.is_subagent: return refuse` lines flips to `pass`, nothing
    fires. This test restores end-to-end coverage of every refuse branch.
    """
    from sali.config.settings import Settings
    from sali.core.clock import SystemClock
    from sali.tools.builtins.agent_tool import AskUser
    from sali.tools.builtins.task_tool import (
        AdvanceTask, FinishTask, ModifyTask, PlanTask, ReviewTask,
    )
    from sali.tools.context import ToolContext

    sub = ToolContext(settings=Settings(), clock=SystemClock(), is_subagent=True)

    # AskUser (agent_tool.py:38)
    r = await AskUser().run({"question": "A or B?"}, sub)
    assert not r.ok, "AskUser must refuse in a subagent context"
    assert "subagent" in (r.error or "").lower()

    # PlanTask (task_tool.py:140) — needs valid args to reach the subagent check
    r = await PlanTask().run({"objective": "x", "steps": ["a"]}, sub)
    assert not r.ok, "PlanTask must refuse in a subagent context"
    assert "subagent" in (r.error or "").lower()

    # AdvanceTask (task_tool.py:213)
    r = await AdvanceTask().run({"task_id": str(uuid4())}, sub)
    assert not r.ok, "AdvanceTask must refuse in a subagent context"
    assert "subagent" in (r.error or "").lower()

    # FinishTask (task_tool.py:323)
    r = await FinishTask().run({"task_id": str(uuid4()), "status": "done"}, sub)
    assert not r.ok, "FinishTask must refuse in a subagent context"
    assert "subagent" in (r.error or "").lower()

    # ReviewTask (task_tool.py:384)
    r = await ReviewTask().run({"task_id": str(uuid4())}, sub)
    assert not r.ok, "ReviewTask must refuse in a subagent context"
    assert "subagent" in (r.error or "").lower()

    # ModifyTask (task_tool.py:501)
    r = await ModifyTask().run({"task_id": str(uuid4()), "action": "edit_step"}, sub)
    assert not r.ok, "ModifyTask must refuse in a subagent context"
    assert "subagent" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_ask_user_wrapper_covers_all_four_branches() -> None:
    """Fold-in: adversarial review noted that after Turn 8 removed the delegation tests, the 4-way
    branch in `AskUser.run` (subagent refuse, no-clarify refuse, empty-question refuse, happy
    passthrough) had lost its end-to-end coverage. Restore it here."""
    from sali.config.settings import Settings
    from sali.core.clock import SystemClock
    from sali.tools.builtins.agent_tool import AskUser
    from sali.tools.context import ToolContext

    class _FakeClarify:
        def __init__(self, ok: bool = True) -> None:
            self._ok = ok

        async def ask(self, question: str) -> dict:
            return {"ok": self._ok, "question_id": "qid-1", "status": "waiting_for_user"} \
                if self._ok else {"ok": False, "reason": "no task"}

    # (1) subagent refuse
    sub = ToolContext(settings=Settings(), clock=SystemClock(), is_subagent=True,
                      clarify=_FakeClarify())
    r = await AskUser().run({"question": "A?"}, sub)
    assert not r.ok and "subagent" in (r.error or "").lower()

    # (2) no clarify sink
    no_clarify = ToolContext(settings=Settings(), clock=SystemClock(), clarify=None)
    r = await AskUser().run({"question": "A?"}, no_clarify)
    assert not r.ok and "clarification" in (r.error or "").lower()

    # (3) empty question
    ok_ctx = ToolContext(settings=Settings(), clock=SystemClock(), clarify=_FakeClarify())
    r = await AskUser().run({"question": ""}, ok_ctx)
    assert not r.ok and "question is required" in (r.error or "").lower()

    # (4) happy passthrough — ctx.clarify.ask returns ok=True
    r = await AskUser().run({"question": "Deploy to A or B?"}, ok_ctx)
    assert r.ok
    assert r.output["status"] == "waiting_for_user"

    # (5) ctx.clarify.ask returns ok=False — refuse and surface the reason
    fail_ctx = ToolContext(settings=Settings(), clock=SystemClock(), clarify=_FakeClarify(ok=False))
    r = await AskUser().run({"question": "Deploy?"}, fail_ctx)
    assert not r.ok and "no task" in (r.error or "").lower()
