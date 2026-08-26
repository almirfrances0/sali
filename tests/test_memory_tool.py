"""The remember tool (§10-12): source mapping + provenance, and its guards."""

from __future__ import annotations

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemorySource
from sali.tools.builtins.memory_tool import RememberFact
from sali.tools.context import ToolContext


class _FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, MemorySource, str | None, float]] = []

    async def remember(self, content: str, *, source: MemorySource, note: str | None = None,
                       importance: float = 0.6) -> None:
        self.calls.append((content, source, note, importance))


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
