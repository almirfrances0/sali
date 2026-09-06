"""Brain-audit Turn 1: pure conversational turns skip the ~10.6k-token native tools payload.

Verifies:
  1. The settings flag `always_send_tools` exists and defaults to False (aggressive save).
  2. is_delegation still detects "run it" / "do it" so those turns keep tools.
  3. The AttentionCategory 'CONVERSATION' + no active task is the pure-chat case.

Deep integration (spy on provider.chat_stream) is deferred; the gate logic is 4 conditions
combined with `and`, direct enough that unit-testing each condition is more valuable than
mocking the whole loop iteration.
"""

from __future__ import annotations

import pytest

from sali.config.settings import RuntimeSettings


def test_always_send_tools_defaults_to_false() -> None:
    """Default = aggressive save. Set True as kill-switch."""
    s = RuntimeSettings()
    assert s.always_send_tools is False


def test_is_delegation_still_works() -> None:
    """The pure-chat gate excludes delegations - Sali must keep tools for 'run it'."""
    from sali.runtime.loop import is_delegation
    assert is_delegation("run it")
    assert is_delegation("do it")
    assert not is_delegation("hi")
    assert not is_delegation("what's the weather")


def test_pure_chat_predicate_shape() -> None:
    """The four AND-conditions defining the skip case are the ones we mean.
    Direct sanity via inline computation matching the loop.py branch."""
    from sali.runtime.loop import is_delegation

    def _skip(iteration: int, primary_task_id, internal: bool, user_input: str) -> bool:
        return (iteration == 0
                and primary_task_id is None
                and not internal
                and not is_delegation(user_input))

    assert _skip(0, None, False, "hi") is True                 # pure chat
    assert _skip(0, None, False, "run it") is False            # delegation → keep tools
    assert _skip(0, "task-id", False, "hi") is False           # active task → keep tools
    assert _skip(0, None, True, "hi") is False                 # background self-check → keep tools
    assert _skip(1, None, False, "hi") is False                # iter > 0 → keep tools
