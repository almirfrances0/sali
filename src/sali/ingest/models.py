"""Document-ingestion value objects (§44)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IngestResult:
    path: str
    status: str  # ok | unchanged | unsupported | empty | error
    chunks: int = 0
    bytes: int = 0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "unchanged")
