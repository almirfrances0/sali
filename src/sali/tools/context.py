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
        importance: float = 0.6, needs_grounding: bool = False, about: str | None = None,
    ) -> None: ...


class GraphSink(Protocol):
    """How a tool records a relationship (subject --relation--> object) into Sali's knowledge graph.
    Concrete impl (GraphService) is injected by the runtime; tools see only this capability, so the
    graph layer stays out of the tools layer's imports (same inversion as MemorySink)."""

    async def link(
        self, *, subject: str, relation: str, obj: str, source: MemorySource,
        confidence: float = 0.6,
    ) -> Any: ...  # concrete returns the Edge; tools ignore it


class TaskSink(Protocol):
    """How a tool records and advances a persistent multi-step task (§24). Concrete impl (TaskStore)
    is injected by the runtime; tasks survive restarts because they live in the datastore."""

    async def create(self, objective: str, steps: list[str]) -> Any: ...
    async def current(self) -> Any: ...  # the task Sali is working on now, or None
    async def advance(
        self, task_id: Any, step_seq: int, status: str, *, note: str | None = None
    ) -> Any: ...
    async def finish(self, task_id: Any, *, status: str = "done", result: str | None = None) -> None: ...


@dataclass(slots=True)
class ToolContext:
    settings: Settings
    clock: Clock
    pool: Any = None
    session_id: UUID | None = None
    memory: MemorySink | None = None  # injected by the loop; None in tests / pool-less probes
    graph: GraphSink | None = None  # injected by the loop; lets a tool assert a relationship
    tasks: TaskSink | None = None  # injected by the loop; lets a tool run a persistent task

    @property
    def paths(self) -> PathGuard:
        return PathGuard(self.settings.permissions)


def local_context(settings: Settings | None = None) -> ToolContext:
    """A pool-less context for tools that only touch the filesystem/process (tests, CLI probes)."""
    return ToolContext(settings=settings or Settings(), clock=SystemClock())
