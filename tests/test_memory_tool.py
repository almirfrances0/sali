"""The remember tool (§10-12): source mapping + provenance, and its guards."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemorySource
from sali.tools.builtins.memory_tool import RememberFact
from sali.tools.context import ToolContext


class _FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, MemorySource, str | None, float]] = []
        self.grounding: list[bool] = []

    async def remember(self, content: str, *, source: MemorySource, note: str | None = None,
                       importance: float = 0.6, needs_grounding: bool = False,
                       about: str | None = None) -> None:
        self.calls.append((content, source, note, importance))
        self.grounding.append(needs_grounding)


def _ctx(sink: _FakeSink | None) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), memory=sink)


async def test_remember_maps_source_and_records_provenance() -> None:
    sink = _FakeSink()
    ctx = _ctx(sink)

    web = await RememberFact().run(
        {"content": "ripgrep is written in Rust", "source": "web",
         "url": "https://github.com/BurntSushi/ripgrep"}, ctx)
    assert web.ok
    content, source, note, imp = sink.calls[0]
    assert source is MemorySource.EXTERNAL_SOURCE  # online → external observation, not settled fact
    assert note and "github.com" in note  # provenance recorded (§10)
    assert sink.grounding[0] is True  # a web fact is flagged needs_grounding (unverified until checked)

    await RememberFact().run({"content": "Almir prefers Neovim", "source": "user"}, ctx)
    # The model can't mint USER_EXPLICIT — a fact it chooses to keep from the chat is CONVERSATION,
    # and NOT weighted above a web finding (golden rule: never treat an LLM reply as ground truth).
    assert sink.calls[1][1] is MemorySource.CONVERSATION
    assert sink.calls[1][3] <= imp

    await RememberFact().run({"content": "probably worth trying X", "source": "inference"}, ctx)
    assert sink.calls[2][1] is MemorySource.INFERENCE  # Sali's own conclusion → weaker


async def test_remember_guards_missing_memory_and_empty_content() -> None:
    no_mem = await RememberFact().run({"content": "x"}, _ctx(None))
    assert not no_mem.ok and "memory" in (no_mem.error or "").lower()

    empty = await RememberFact().run({"content": "   "}, _ctx(_FakeSink()))
    assert not empty.ok and "content" in (empty.error or "").lower()


@pytest.mark.db
async def test_remember_about_supersedes_a_single_valued_fact(live_pool: Any) -> None:
    # Restating a fact under the same `about` topic REPLACES the old value (functional claim) instead
    # of leaving two contradictory memories current — memory stays coherent.
    from sali.memory.service import MemoryService
    from sali.provider.fake import FakeModelProvider
    from sali.runtime.loop import _MemorySink

    sink = _MemorySink(MemoryService(live_pool, FakeModelProvider()))
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), memory=sink)
    await RememberFact().run(
        {"content": "Almir's editor is Neovim", "source": "user", "about": "editor"}, ctx)
    await RememberFact().run(
        {"content": "Almir's editor is Helix now", "source": "user", "about": "editor"}, ctx)

    async with live_pool.acquire() as c:
        rows = await c.fetch(
            "SELECT content FROM memory WHERE claim_key='remember:editor' AND valid_until IS NULL")
    assert len(rows) == 1 and "Helix" in rows[0]["content"]  # one current value, the newest
