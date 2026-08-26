"""The run state machine and crash-resume decision.

Every turn walks these states, journaled write-ahead before any side effect. On restart, a
run left mid-flight is resolved by :func:`resume_action`: a non-idempotent tool that was
executing must be *verified against reality*, never blindly re-run (fix M15); an inconclusive
case is surfaced to the user, never guessed as success.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    INPUT = "input"
    UNDERSTAND = "understand"
    RETRIEVE = "retrieve"
    BUILD_CONTEXT = "build_context"
    REASON_PLAN = "reason_plan"
    AWAIT_CONFIRM = "await_confirm"
    EXECUTE_TOOL = "execute_tool"
    OBSERVE = "observe"
    VERIFY = "verify"
    UPDATE_STATE = "update_state"
    TOOL_DENIED = "tool_denied"
    LEARN = "learn"
    FINALIZE = "finalize"
    RESPOND = "respond"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


class ResumeAction(StrEnum):
    REPLAY_SAFE = "replay_safe"  # no side effect was in flight → safe to re-drive
    VERIFY_THEN_CONTINUE = "verify_then_continue"  # non-idempotent effect in flight → check reality
    ABORT_SURFACE = "abort_surface"  # unknown → surface to the user, never guess


def resume_action(state: RunState, executing_tool_idempotent: bool | None) -> ResumeAction:
    """Decide how to recover a run that was ``running`` when the process died."""
    if state is not RunState.EXECUTE_TOOL:
        return ResumeAction.REPLAY_SAFE  # nothing had committed a side effect yet
    if executing_tool_idempotent is True:
        return ResumeAction.REPLAY_SAFE
    if executing_tool_idempotent is False:
        return ResumeAction.VERIFY_THEN_CONTINUE
    return ResumeAction.ABORT_SURFACE
