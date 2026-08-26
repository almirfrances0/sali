"""Execution context handed to every tool.

Carries what a tool may legitimately need — the settings (permission scoping), a clock, and
the pool — without letting the tool reach for globals. The PathGuard is derived from the
permission settings so filesystem/exec tools confine themselves consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sali.config.settings import Settings
from sali.core.clock import Clock, SystemClock
from sali.core.enums import MemorySource
from sali.tools.pathguard import PathGuard


class MemorySink(Protocol):
    """How a tool asks Sali to remember something durably. The concrete implementation lives above
    the tools layer (runtime injects it); tools only see this abstract capability, so the memory
    system stays out of the tools layer's imports."""

    async def remember(
        self, content: str, *, source: MemorySource, note: str | None = None,
        importance: float = 0.6,
    ) -> None: ...


@dataclass(slots=True)
class ToolContext:
    settings: Settings
    clock: Clock
    pool: Any = None
    session_id: UUID | None = None
    memory: MemorySink | None = None  # injected by the loop; None in tests / pool-less probes

    @property
    def paths(self) -> PathGuard:
        return PathGuard(self.settings.permissions)


def local_context(settings: Settings | None = None) -> ToolContext:
    """A pool-less context for tools that only touch the filesystem/process (tests, CLI probes)."""
    return ToolContext(settings=settings or Settings(), clock=SystemClock())
