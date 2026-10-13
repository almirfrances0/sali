"""The perception contract + value types (all local, all read-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class WindowInfo:
    """The focused window: which app, its title, pid, and screen geometry."""

    app: str
    title: str
    pid: int | None = None
    geometry: tuple[int, int, int, int] | None = None  # x, y, w, h

    def to_dict(self) -> dict[str, Any]:
        return {"app": self.app, "title": self.title, "pid": self.pid,
                "geometry": list(self.geometry) if self.geometry else None}


@dataclass(slots=True)
class UiNode:
    """One node of the accessibility tree: its role, label, and — for text-bearing roles — contents.

    `text` is empty for everything except genuine text surfaces (a terminal buffer, a document, an
    entry), and is NEVER populated for a password field. It arrives already scrubbed by
    security/redact, which masks credential shapes and leaves ordinary text alone.
    """

    role: str
    name: str
    text: str = ""
    children: list[UiNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "name": self.name}
        if self.text:
            out["text"] = self.text
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        return out


class Perception(Protocol):
    """Sali's read-only view of the desktop. `snapshot` returns a JSON-ready dict so the tools layer
    needs no perception types (structural, like the other sinks). Never raises — an unusable backend
    reports ``available=False`` with a human ``detail`` instead."""

    async def snapshot(self, *, ui: bool = False) -> dict[str, Any]: ...
