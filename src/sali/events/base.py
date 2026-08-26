"""Event-engine value types (all local, all read-only observation)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class EventKind(StrEnum):
    FILE_CREATED = "file_created"
    FILE_MODIFIED = "file_modified"
    FILE_DELETED = "file_deleted"
    FILE_MOVED = "file_moved"
    WINDOW_FOCUS = "window_focus"


@dataclass(slots=True)
class DesktopEvent:
    """One raw desktop event before filtering/scoring. `target` is a path (fs) or an app name (window)."""

    kind: EventKind
    target: str
    at: datetime
    source: str = ""  # which EventSource produced it (for diagnostics)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Observation:
    """A scored, possibly-aggregated observation — what the engine surfaces. A burst of related raw
    events collapses into ONE of these with a `count`. Carries NO raw secret/screenshot (§27, §30)."""

    kind: EventKind
    summary: str
    importance: float
    count: int
    first_at: datetime
    last_at: datetime
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "summary": self.summary, "importance": round(self.importance, 3),
                "count": self.count, "first_at": self.first_at.isoformat(),
                "last_at": self.last_at.isoformat(), "detail": self.detail}


class ObservationSink(Protocol):
    """Where surfaced observations go. The default sink logs + keeps a bounded buffer; a later fusion
    phase (§7) swaps in a sink that feeds the twin/graph/memory. Never triggers an action (§46)."""

    async def observe(self, obs: Observation) -> None: ...
