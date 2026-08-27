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
    hops: int = 1  # graph distance from the query's seed entity (1 = direct, 2 = one step further)


@dataclass(slots=True)
class RecentItem:
    event_type: str
    at: datetime


@dataclass(slots=True)
class ToolFact:
    """Tools on the machine relevant to a question — either the providers of a capability
    (capability = a slug) or a general inventory summary (capability = 'inventory')."""

    capability: str
    description: str
    tools: list[str]


@dataclass(slots=True)
class RetrievalBundle:
    memories: list[MemoryHit] = field(default_factory=list)
    graph_facts: list[GraphFact] = field(default_factory=list)
    recent: list[RecentItem] = field(default_factory=list)
    tool_facts: list[ToolFact] = field(default_factory=list)
    procedures: list[MemoryHit] = field(default_factory=list)   # how Sali handled this before (§4)
    experiences: list[MemoryHit] = field(default_factory=list)  # past incidents/episodes (§6)
