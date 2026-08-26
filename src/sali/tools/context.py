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
from sali.tools.remote import RemoteRunner


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


class ScheduleSink(Protocol):
    """How a tool sets up or cancels a recurring job (§44). Concrete impl (ScheduleStore) is injected
    by the runtime; the schedule is durable and fires as a Sali turn at its cron/interval time."""

    async def create(self, name: str, when: str, prompt: str) -> Any: ...
    async def delete(self, name: str) -> int: ...


class IngestSink(Protocol):
    """How a tool ingests a document into memory (§44). Concrete impl (IngestService) is injected by
    the runtime; the chunks become citeable FILE_OBSERVATION memories."""

    async def ingest(self, path: str) -> Any: ...


class CommsSink(Protocol):
    """How a tool reads/sends email and reads/writes the calendar (§44). Concrete impl (CommsService)
    is injected by the runtime; secrets are resolved at connect time, never stored."""

    async def email_search(self, query: str = "", *, limit: int = 20) -> Any: ...
    async def email_read(self, uid: str) -> Any: ...
    async def email_send(self, to: str, subject: str, body: str) -> str: ...
    async def calendar_list(self, start: Any, end: Any) -> Any: ...
    async def calendar_add(self, summary: str, start: Any, end: Any, *, location: str | None = None) -> str: ...


class BrowserSink(Protocol):
    """How a tool drives Sali's browser (§44). Concrete impl (FakeBrowser / PlaywrightFirefox) is
    injected by the runtime; it holds Sali's own Firefox, seeded with Almir's login cookies."""

    async def open(self, url: str) -> Any: ...
    async def read(self) -> Any: ...
    async def click(self, selector: str) -> Any: ...
    async def fill(self, selector: str, value: str) -> Any: ...
    async def screenshot(self, path: str) -> str: ...


class VisionSink(Protocol):
    """How a tool has Sali LOOK at an image (a screenshot) and reason over it (sali3 §33-35). The
    concrete impl (over the local vision model) is injected by the runtime; the image goes only to
    the local model and is never stored/logged — the tools layer never imports the provider."""

    async def look(self, prompt: str, image: bytes) -> str: ...


@dataclass(slots=True)
class ToolContext:
    settings: Settings
    clock: Clock
    pool: Any = None
    session_id: UUID | None = None
    memory: MemorySink | None = None  # injected by the loop; None in tests / pool-less probes
    graph: GraphSink | None = None  # injected by the loop; lets a tool assert a relationship
    tasks: TaskSink | None = None  # injected by the loop; lets a tool run a persistent task
    schedules: ScheduleSink | None = None  # injected by the loop; lets a tool set up recurring work
    documents: IngestSink | None = None  # injected by the loop; lets a tool ingest a document
    remote: RemoteRunner | None = None  # injected by the loop; lets a tool run on a remote host
    comms: CommsSink | None = None  # injected by the loop; lets a tool read/send email + calendar
    browser: BrowserSink | None = None  # injected by the loop; lets a tool drive Sali's browser
    vision: VisionSink | None = None  # injected by the loop; lets a tool look at the screen locally

    @property
    def paths(self) -> PathGuard:
        return PathGuard(self.settings.permissions)


def local_context(settings: Settings | None = None) -> ToolContext:
    """A pool-less context for tools that only touch the filesystem/process (tests, CLI probes)."""
    return ToolContext(settings=settings or Settings(), clock=SystemClock())
