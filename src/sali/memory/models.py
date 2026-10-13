"""Memory value objects and row mapping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sali.core.enums import FreshnessPolicy, MemoryLayer, MemorySource


@dataclass(slots=True)
class Memory:
    id: UUID
    layer: MemoryLayer
    content: str
    source: MemorySource
    confidence: float
    importance: float
    reliability: float
    evidence_count: int
    freshness: FreshnessPolicy
    valid_from: datetime
    valid_until: datetime | None
    last_verified: datetime
    embed_status: str
    structured: dict[str, Any]
    claim_key: str | None = None
    functional: bool = False
    needs_grounding: bool = False


@dataclass(slots=True)
class MemoryHit:
    """A retrieved memory with the scores Sali ranks and *labels* it by."""

    memory: Memory
    score: float
    effective_confidence: float
    freshness_factor: float
    stale: bool
    similarity: float | None
    retriever: str  # "vector" | "keyword" | "hybrid"


def row_to_memory(row: Any) -> Memory:
    return Memory(
        id=row["id"],
        layer=MemoryLayer(row["layer"]),
        content=row["content"],
        source=MemorySource(row["source"]),
        confidence=row["confidence"],
        importance=row["importance"],
        reliability=row["reliability"],
        evidence_count=row["evidence_count"],
        freshness=FreshnessPolicy(row["freshness"]),
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        last_verified=row["last_verified"],
        embed_status=row["embed_status"],
        structured=row["structured"],
        claim_key=row["claim_key"],
        functional=row["functional"],
        needs_grounding=row["needs_grounding"],
    )
