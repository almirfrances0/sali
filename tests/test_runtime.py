"""Crash-resume decisions and the built-in R0 tools."""

from __future__ import annotations

from typing import Any

from sali.runtime.state import ResumeAction, RunState, resume_action
from sali.tools.builtins.system import DiskInfo, MemoryInfo, SystemInfo
from sali.tools.dispatch import run_tool


def test_resume_non_idempotent_verifies_not_reruns() -> None:
    # fix M15: a non-idempotent effect in flight must be checked against reality, not re-run.
    assert resume_action(RunState.EXECUTE_TOOL, False) is ResumeAction.VERIFY_THEN_CONTINUE


def test_resume_idempotent_is_replay_safe() -> None:
    assert resume_action(RunState.EXECUTE_TOOL, True) is ResumeAction.REPLAY_SAFE


def test_resume_before_execute_is_replay_safe() -> None:
    assert resume_action(RunState.REASON_PLAN, None) is ResumeAction.REPLAY_SAFE


def test_resume_unknown_tool_surfaces() -> None:
    assert resume_action(RunState.EXECUTE_TOOL, None) is ResumeAction.ABORT_SURFACE


async def test_memory_info_tool_reads_live(tctx: Any) -> None:
    result = await run_tool(MemoryInfo(), {}, tctx)
    assert result.ok
    assert result.output["total_mib"] > 0
    verify = await MemoryInfo().verify({}, result)
    assert verify.success


async def test_disk_and_system_tools(tctx: Any) -> None:
    disk = await run_tool(DiskInfo(), {}, tctx)
    assert disk.ok and disk.output["total_gib"] > 0
    system = await run_tool(SystemInfo(), {}, tctx)
    assert system.ok and system.output["kernel"]


async def test_missing_required_arg_is_rejected_pre_effect(tctx: Any) -> None:
    class NeedsArg(SystemInfo):
        name = "needs_arg"
        parameters = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    result = await run_tool(NeedsArg(), {}, tctx)  # JSON-Schema validation runs pre-effect
    assert not result.ok
    assert "required" in (result.error or "").lower()
