"""Retrieval result value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sali.memory.models import MemoryHit


@dataclass(slots=True)
class GraphFact:
    src: str
    rel: str
    dst: str
    confidence: float


@dataclass(slots=True)
class RecentItem:
    event_type: str
    at: datetime


@dataclass(slots=True)
class RetrievalBundle:
    memories: list[MemoryHit] = field(default_factory=list)
    graph_facts: list[GraphFact] = field(default_factory=list)
    recent: list[RecentItem] = field(default_factory=list)
