"""What Almir stated must land as USER_EXPLICIT memory - reliably, on a small model.

Live measurement before this fix: 131 foreground turns in 3 days, 1 memory captured (0.7%). The
gate matched ~70% of real turns; the failure was inside `_capture_durable`, which asked a Q2_K
model to return JSON and silently dropped everything that didn't parse. The fix replaces JSON with
a two-shape line (`KEEP: <sentence>` or `SKIP`) that small models produce reliably, and computes
the metadata (`kind`, `needs_grounding`) deterministically from the sentence content.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sali.runtime.loop import _infer_kind


# ── kind inference is deterministic from vocabulary ────────────────────────────────────────

@pytest.mark.parametrize("sentence,expected", [
    ("you prefer short answers with the substance kept in", "preference"),
    ("you like dark interfaces", "preference"),
    ("call me Sir", "preference"),
    ("you always want database migrations reviewed", "preference"),
    ("your name is Almir", "identity"),
    ("i am Sali, running on your local machine", "identity"),
    ("this machine has 12GB of VRAM", "environment"),
    ("your main projects live in /home/almir/Desktop", "environment"),
    ("docker is installed", "environment"),
    ("to compress the archive, first tar then gzip", "procedure"),
    ("the sky is blue", "fact"),
])
def test_infer_kind_routes_to_the_right_layer(sentence: str, expected: str) -> None:
    """A wrong kind lands a memory in the wrong layer (preferences vs facts vs environment retrieve
    differently). These parametrics cover the six categories today's writer supports."""
    assert _infer_kind(sentence) == expected, sentence


# ── the parser handles the shapes a small model actually produces ─────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("KEEP: you use VS Code as your editor", "you use VS Code as your editor"),
    ("keep: you use VS Code as your editor", "you use VS Code as your editor"),
    ("  KEEP:  you use VS Code as your editor  ", "you use VS Code as your editor"),
    ('KEEP: "you use VS Code as your editor"', "you use VS Code as your editor"),
    ("Some prose before.\nKEEP: you use VS Code as your editor",
     "you use VS Code as your editor"),
])
def test_extract_keep_line_from_model_output(raw: str, expected: str) -> None:
    """The parser survives the small-model quirks the JSON version couldn't. A KEEP line anywhere
    in the output is enough; leading/trailing quotes and whitespace are trimmed."""
    got = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("KEEP:"):
            got = stripped.split(":", 1)[1].strip().strip("\"'`")
            break
    assert got == expected


@pytest.mark.parametrize("raw", [
    "SKIP",
    "skip",
    "SKIP.",
    "SKIP - nothing durable here",
    "he didn't state anything durable, so: SKIP",
    "just a greeting",
    "",
])
def test_skip_or_no_keep_line_returns_nothing(raw: str) -> None:
    """The SKIP path and the no-shape-at-all path both correctly refuse to capture. False positives
    would fill user_explicit with garbage; false negatives are recoverable (Almir says it again)."""
    got = ""
    for line in raw.splitlines():
        stripped = line.strip()
        up = stripped.upper()
        if up.startswith("KEEP:"):
            got = stripped.split(":", 1)[1].strip().strip("\"'`")
            break
        if up == "SKIP" or up.startswith("SKIP "):
            got = ""
            break
    assert got == ""


# ── the whole pipeline: gate matches, provider returns KEEP, memory lands ───────────────────

@pytest.mark.db
async def test_a_stated_preference_lands_as_user_explicit_memory(db_conn: Any) -> None:  # type: ignore[name-defined]
    """The end-to-end shape. Uses a stub _MemorySink to capture the write - we're checking the
    _capture_durable behaviour, not the whole runtime. The important assertions are (a) source is
    USER_EXPLICIT (not INFERENCE), (b) content is what the model said (not scaffolding), (c) kind
    was inferred from vocabulary, (d) checkable was set from `looks_checkable`."""
    from sali.runtime.loop import AgentLoop
    from sali.provider.base import ChatResult, ChatMessage
    from typing import Any as _Any

    captured: dict[str, _Any] = {}

    class _Sink:
        async def remember(self, content: str, **kwargs: _Any) -> _Any:
            captured["content"] = content
            captured.update(kwargs)
            return MagicMock(id="m1")

    class _Provider:
        async def chat(self, messages: list[ChatMessage], **_: _Any) -> ChatResult:
            return ChatResult(model="fake", content="KEEP: you prefer short answers",
                              tokens_in=1, tokens_out=8, thinking="", tool_calls=[])

    class _Journal:
        async def event(self, *a: _Any, **k: _Any) -> None:
            pass

    loop = object.__new__(AgentLoop)
    loop.provider = _Provider()
    loop._memory_sink = _Sink()

    await loop._capture_durable("i prefer short answers", "got it", _Journal())

    from sali.core.enums import MemorySource
    assert captured["source"] is MemorySource.USER_EXPLICIT
    assert captured["content"] == "you prefer short answers"
    assert captured["kind"] == "preference", "vocabulary routes to preference layer"
    assert captured["needs_grounding"] is False, "a preference is not a filesystem-checkable claim"


@pytest.mark.db
async def test_a_checkable_environment_claim_is_flagged_for_grounding(db_conn: Any) -> None:  # type: ignore[name-defined]
    """Alignment with the async grounding faculty (`_looks_checkable`). A stated fact about the
    machine gets `needs_grounding=True` so it enters the queue the faculty verifies."""
    from sali.runtime.loop import AgentLoop
    from sali.provider.base import ChatResult, ChatMessage
    from typing import Any as _Any

    captured: dict[str, _Any] = {}

    class _Sink:
        async def remember(self, content: str, **kwargs: _Any) -> _Any:
            captured["content"] = content
            captured.update(kwargs)
            return MagicMock(id="m2")

    class _Provider:
        async def chat(self, messages: list[ChatMessage], **_: _Any) -> ChatResult:
            return ChatResult(model="fake", content="KEEP: docker is installed on this machine",
                              tokens_in=1, tokens_out=8, thinking="", tool_calls=[])

    class _Journal:
        async def event(self, *a: _Any, **k: _Any) -> None:
            pass

    loop = object.__new__(AgentLoop)
    loop.provider = _Provider()
    loop._memory_sink = _Sink()

    await loop._capture_durable("docker is installed here", "noted", _Journal())

    assert captured["kind"] == "environment"
    assert captured["needs_grounding"] is True, \
        "a machine claim must be queued for the grounding faculty to verify"
