"""Graph value objects and row mapping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sali.core.enums import MemorySource


@dataclass(slots=True)
class Node:
    id: UUID
    node_type: str
    name: str
    canonical_key: str
    props: dict[str, Any]
    source: MemorySource
    confidence: float
    valid_from: datetime
    valid_until: datetime | None
    last_verified: datetime
    last_seen: datetime


@dataclass(slots=True)
class Edge:
    id: UUID
    src_id: UUID
    dst_id: UUID
    rel_type: str
    props: dict[str, Any]
    source: MemorySource
    confidence: float
    valid_from: datetime
    valid_until: datetime | None
    last_verified: datetime


@dataclass(slots=True)
class ContradictionOutcome:
    winner: str  # 'new' | 'old'
    resolution: str  # 'new_wins' | 'old_wins' | 'recency_tiebreak'
    contradiction_id: UUID


def row_to_node(row: Any) -> Node:
    return Node(
        id=row["id"],
        node_type=row["node_type"],
        name=row["name"],
        canonical_key=row["canonical_key"],
        props=row["props"],
        source=MemorySource(row["source"]),
        confidence=row["confidence"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        last_verified=row["last_verified"],
        last_seen=row["last_seen"],
    )


def row_to_edge(row: Any) -> Edge:
    return Edge(
        id=row["id"],
        src_id=row["src_id"],
        dst_id=row["dst_id"],
        rel_type=row["rel_type"],
        props=row["props"],
        source=MemorySource(row["source"]),
        confidence=row["confidence"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        last_verified=row["last_verified"],
    )
