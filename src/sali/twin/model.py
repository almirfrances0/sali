"""The Desktop Digital Twin's data shape (spec §14).

A snapshot is a *structural* picture of the machine — what hardware, software, projects, and
models exist — captured by deterministic observers (§15), never by the model. It is reconciled
into the temporal knowledge graph so Sali knows its own machine structurally and over time.
Volatile metrics (current RAM/disk usage) are deliberately NOT stored here — those are checked
live on demand; the twin holds the stable structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TwinEntity:
    """One structural thing on the machine, related to it by ``relation`` (has/runs/hosts)."""

    kind: str  # graph node_type: hardware | software | project | model | service | network
    key: str  # canonical, hardware-stable identity (e.g. "software:ollama", "project:sali")
    name: str  # human name (e.g. "NVIDIA RTX 4070", "Ollama 0.32.15")
    props: dict[str, Any] = field(default_factory=dict)
    relation: str = "has"  # rel_type from the machine node to this entity


@dataclass(slots=True)
class TwinSnapshot:
    """A whole-machine structural snapshot: the machine node plus everything on it."""

    machine_key: str
    machine_name: str
    machine_props: dict[str, Any] = field(default_factory=dict)
    entities: list[TwinEntity] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[TwinEntity]:
        return [e for e in self.entities if e.kind == kind]
