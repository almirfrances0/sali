"""Execution context handed to every tool.

Carries what a tool may legitimately need — the settings (permission scoping), a clock, and
the pool — without letting the tool reach for globals. The PathGuard is derived from the
permission settings so filesystem/exec tools confine themselves consistently.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

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
        kind: str | None = None,
    ) -> None: ...

    async def forget(self, query: str, *, reason: str) -> dict[str, Any]: ...
    async def verify(self, query: str, *, verified: bool, note: str | None = None) -> dict[str, Any]: ...


class RecallSink(Protocol):
    """How a tool ACTIVELY queries Sali's memory (spec §34,§55,§56) — search, graph traversal,
    history, procedures, incidents. Read-only; returns JSON-ready dicts (with provenance + confidence
    + freshness, so the model never has to guess), so the tools layer stays free of memory/graph
    types. The concrete impl is injected by the runtime over the retrieval + graph engines."""

    async def search(self, query: str, *, layer: str | None = None, k: int = 6) -> list[dict[str, Any]]: ...
    async def related(self, entity: str, *, hops: int = 1) -> dict[str, Any]: ...
    async def entity(self, name: str) -> dict[str, Any]: ...
    async def history(self, entity: str, relation: str) -> dict[str, Any]: ...


class GraphSink(Protocol):
    """How a tool records a relationship (subject --relation--> object) into Sali's knowledge graph.
    Concrete impl (GraphService) is injected by the runtime; tools see only this capability, so the
    graph layer stays out of the tools layer's imports (same inversion as MemorySink)."""

    async def link(
        self, *, subject: str, relation: str, obj: str, source: MemorySource,
        confidence: float = 0.6,
    ) -> Any: ...  # concrete returns the Edge; tools ignore it

    async def reach_host(self, host: str) -> Any: ...  # record a REMOTE host Sali can_access (§9)


class TaskSink(Protocol):
    """How a tool records and advances a persistent multi-step task (§24). Concrete impl (TaskStore)
    is injected by the runtime; tasks survive restarts because they live in the datastore."""

    async def create(self, objective: str, steps: list[Any], **kwargs: Any) -> Any: ...
    async def bind_workspace(  # resolve + persist the task's authoritative workspace (Prompt 5)
        self, task_id: Any, *, objective: str, explicit: str | None,
        sali_works_root: str, cwd: str | None = None,
    ) -> dict[str, Any]: ...
    async def current(self) -> Any: ...  # the task Sali is working on now, or None
    async def advance(
        self, task_id: Any, step_seq: int, status: str, *, note: str | None = None,
        error: str | None = None, verified_by: Any = None, run_id: Any = None,
    ) -> Any: ...
    async def checkpoint(self, task_id: Any, step_seq: int, data: dict[str, Any]) -> None: ...  # resume mid-step
    async def finish(
        self, task_id: Any, *, status: str = "done", result: str | None = None, run_id: Any = None,
    ) -> str | None: ...
    async def record_artifact(
        self, task_id: Any, artifact_path: str, artifact_type: str,
        *, tool_name: str | None = None,
    ) -> None: ...
    async def artifacts(self, task_id: Any) -> list[dict[str, Any]]: ...
    async def current_step(self, task_id: Any) -> int | None: ...


class ResearchSink(Protocol):
    """How a tool does just-in-time web research bound to the current task (Prompt 5 §14). The concrete
    impl (injected by the runtime) searches the web, persists a durable, task-linked finding, and can
    record/promote evidence-aware learning candidates — so research is durable evidence, never temporary
    context and never automatically permanent truth."""

    async def research(self, query: str, *, step_seq: int | None = None) -> dict[str, Any]: ...
    async def record_lesson(
        self, lesson: str, *, source: str | None = None, research_id: str | None = None,
    ) -> dict[str, Any]: ...
    async def promote_verified(self) -> int: ...


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


class PerceptionSink(Protocol):
    """How a tool reads the desktop's semantic state (sali3 §2,8,9): the focused app/window and,
    optionally, its accessibility tree. Concrete impl (DesktopPerception) is injected by the runtime;
    it returns a JSON-ready dict so the tools layer stays free of perception types. All local."""

    async def snapshot(self, *, ui: bool = False) -> dict[str, Any]: ...


class ToolCatalogSink(Protocol):
    """How a tool queries Sali's knowledge of its OWN tools (spec §53/§21): which installed tools can
    do a task (ranked by learned reliability + safety), and a tool's alternatives. Concrete impl is
    injected by the runtime over the tool inventory + capability graph; returns JSON-ready dicts so
    the tools layer stays free of twin/graph types."""

    async def suggest(self, task: str) -> list[dict[str, Any]]: ...
    async def alternatives(self, tool: str) -> list[str]: ...


class SelfSink(Protocol):
    """How a tool reads Sali's own runtime self-model (spec §6/§7/§41/§71): identity, self-knowledge,
    what it's focused on now, the current task, how it last fared, and what it's uncertain about.
    Concrete impl (over sali_state + the memory/task stores) is injected by the runtime; returns a
    JSON-ready dict so the tools layer stays free of runtime/memory types."""

    async def report(self) -> dict[str, Any]: ...


class HealthSink(Protocol):
    """How a tool reads Sali's live subsystem health (spec §51/§52/§53): datastore, model, embedder,
    perception, and internet reachability. Concrete impl (HealthService) is injected by the runtime;
    returns a JSON-ready dict so the tools layer stays free of runtime/provider types."""

    async def report(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class ToolContext:
    settings: Settings
    clock: Clock
    pool: Any = None
    session_id: UUID | None = None
    run_id: UUID | None = None  # the current agent_run — lets a tool link to its own run's executions (§8)
    memory: MemorySink | None = None  # injected by the loop; None in tests / pool-less probes
    recall: RecallSink | None = None  # injected by the loop; lets a tool actively query memory (§34)
    graph: GraphSink | None = None  # injected by the loop; lets a tool assert a relationship
    tasks: TaskSink | None = None  # injected by the loop; lets a tool run a persistent task
    task_authority: Any = None  # injected by the loop; deterministic active-task enforcement
    reviewer: Any = None  # injected by the loop; the deterministic completion gate (TaskReviewer, §8)
    research: Any = None  # injected by the loop; just-in-time task-bound web research (Prompt 5 §14)
    decisions: Any = None  # injected by the loop; the durable decision ledger (Prompt 6 §30)
    phases: Any = None  # injected by the loop; task phases (Prompt 6 §31)
    # `delegate` removed (brain-audit turn 8): Sali is a single executive agent. No subagent runtime,
    # no delegation tool, no ToolContext channel for it. AskUser stays for user-clarification pauses.
    clarify: Any = None  # injected by the loop; ask the user a clarifying question (§44)
    is_subagent: bool = False  # true inside a bounded subagent run — it must not manage tasks (§16)
    internal: bool = False  # true on an autonomy/continuation/internal turn (not a live user message);
    # lets a tool refuse actions that only a present, intentional user should trigger — e.g. plan_task's
    # dead-work guard, which stops the autonomy loop resurrecting an objective Almir abandoned/completed.
    workspace: Any = None  # injected by the loop; TaskWorkspace for the active task (None = no workspace)
    schedules: ScheduleSink | None = None  # injected by the loop; lets a tool set up recurring work
    documents: IngestSink | None = None  # injected by the loop; lets a tool ingest a document
    remote: RemoteRunner | None = None  # injected by the loop; lets a tool run on a remote host
    comms: CommsSink | None = None  # injected by the loop; lets a tool read/send email + calendar
    browser: BrowserSink | None = None  # injected by the loop; lets a tool drive Sali's browser
    vision: VisionSink | None = None  # injected by the loop; lets a tool look at the screen locally
    perception: PerceptionSink | None = None  # injected by the loop; the focused app/window + UI tree
    catalog: ToolCatalogSink | None = None  # injected by the loop; lets a tool query its own toolset
    self_model: SelfSink | None = None  # injected by the loop; lets a tool read Sali's own self-state
    health: HealthSink | None = None  # injected by the loop; lets a tool read subsystem/internet health

    @property
    def paths(self) -> PathGuard:
        """Anchor relative paths to the RIGHT default so ordinary work stays where Almir can find it.

        Almir, watching Sali land files across his home: "sali have a freedom to write anywhere but
        when working on project he must use his folder so easy for me to track it." The daemon's CWD
        is opaque (systemd starts it wherever), so a relative path like `create_file("foo.py", ...)`
        used to resolve to `/foo.py` and be rejected by the write-root guard - forcing the model to
        supply an absolute path, which it invented freely under `~/`. Files landed anywhere.

        Ladder (most specific first): the active task's workspace (deterministic, unchanged), then
        Sali's canonical workspace root (`permissions.workspace` = ~/Desktop/sali-works). Absolute
        paths still resolve as themselves, so freedom is preserved for anything Sali or Almir names
        explicitly - the change is only what a bare name means when no context says otherwise."""
        base = None
        ws = getattr(self, "workspace", None)
        if ws is not None:
            base = getattr(ws, "workspace_root", None)
        if base is None:
            with contextlib.suppress(Exception):
                base = Path(self.settings.permissions.workspace).expanduser()
                base.mkdir(parents=True, exist_ok=True)
        return PathGuard(self.settings.permissions, base)


def local_context(settings: Settings | None = None) -> ToolContext:
    """A pool-less context for tools that only touch the filesystem/process (tests, CLI probes)."""
    return ToolContext(settings=settings or Settings(), clock=SystemClock())
