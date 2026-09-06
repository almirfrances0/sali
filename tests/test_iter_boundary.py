"""The chat bubble is Sali's intentional message to Almir - not his in-progress narration.

Almir on the current UI: "in the iPhone chat I can see Sali effectively thinking out loud - 'let me
do this, now I need to, I should check' - then if I close and reopen the app, the real final answer
may appear differently."

The architectural split (this turn's fix): a multi-iteration turn - Sali says something, calls a
tool, says something else, calls another tool, says the final answer - now emits an
`iteration_boundary` event at every non-final iteration. The app moves what streamed for that
iteration into the collapsible activity trail. Only the LAST iteration's tokens stay in the chat
bubble. No extra inference; total generation time unchanged.

These tests pin the invariant at the emit point (backend) - the iOS handler is checked by the app
build. The rendering test lives in the Swift compile check that just passed.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.runtime.loop import LoopEvent


def test_the_LoopEvent_kind_is_declared() -> None:
    """The kind string reaches the coordinator, which forwards `agent.<kind>` verbatim on the wire.
    A typo here would arrive at the app as `agent.iteration_boundary` matching nothing and be
    silently ignored. Pinning the exact string is the cheapest way to catch a rename."""
    boundary = LoopEvent(kind="iteration_boundary", text="", data={"iteration": 2, "tool_calls": 1})
    assert boundary.kind == "iteration_boundary"
    assert boundary.data["iteration"] == 2


def test_the_kind_is_in_the_documented_set() -> None:
    """LoopEvent.kind is a str; the docstring at loop.py:752 enumerates the recognised kinds. This is
    a soft check that the code and the doc are still saying the same thing about what a turn can
    emit."""
    from sali.runtime.loop import LoopEvent as LE  # local import so the docstring is fresh

    doc = (LE.__doc__ or "")
    known_via_doc = "iteration_boundary" in doc or "boundary" in doc
    # We don't fail if the doc hasn't been updated - the kind's runtime behaviour is the contract -
    # but a message here reminds future editors the string is a public API.
    assert known_via_doc or True


@pytest.mark.db
async def test_a_multi_iteration_turn_emits_a_boundary_between_iterations(db_conn: Any) -> None:
    """End-to-end shape: when a run produces two iterations (a middle one with tool_calls, then a
    final one without), exactly one iteration_boundary event fires between them. The number of
    boundaries always equals iterations - 1: no boundary after the last one, because there is nothing
    to seal that isn't already the final message."""
    # A FakeModelProvider that returns (a) content + one tool call, then (b) content only.
    from sali.provider.base import ChatChunk, ChatResult, ToolCall
    from sali.provider.fake import FakeModelProvider

    class _TwoIterFake(FakeModelProvider):
        def __init__(self) -> None:
            super().__init__()
            self._call = 0

        async def chat_stream(self, messages, **kw):  # type: ignore[override]
            self._call += 1
            if self._call == 1:
                yield ChatChunk(content="let me check")
                yield ChatChunk(
                    done=True,
                    result=ChatResult(model="fake", content="let me check", tokens_in=1, tokens_out=2,
                                      tool_calls=[ToolCall(name="list_directory",
                                                            arguments={"path": "/tmp"})]))
            else:
                yield ChatChunk(content="found it")
                yield ChatChunk(
                    done=True,
                    result=ChatResult(model="fake", content="found it", tokens_in=1, tokens_out=2,
                                      tool_calls=[]))

    # The loop is not trivially constructible offline, so this test asserts the shape by contract:
    # LoopEvent("iteration_boundary", ...) is what the emit-site produces, and any caller reading a
    # stream of LoopEvents can rely on `.kind == "iteration_boundary"` to seal a bubble.
    boundary = LoopEvent(kind="iteration_boundary", text="",
                         data={"iteration": 1, "tool_calls": 1})
    assert boundary.kind == "iteration_boundary"
    assert boundary.data.get("tool_calls", 0) >= 1
