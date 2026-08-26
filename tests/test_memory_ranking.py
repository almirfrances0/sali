"""Reranking (§28 decay, §29 reactivation, §37): more than similarity decides a memory's rank."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider

_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def _row(*, importance: float, access_count: int, half_life_s: float = 86_400 * 30,
         last_verified: datetime = _NOW) -> dict[str, Any]:
    return {
        "id": uuid4(), "layer": "semantic", "content": "x", "source": "user_explicit",
        "confidence": 0.8, "importance": importance, "reliability": 1.0, "evidence_count": 1,
        "freshness": "permanent", "valid_from": _NOW, "valid_until": None, "last_verified": last_verified,
        "embed_status": "done", "structured": {}, "claim_key": None, "functional": False,
        "needs_grounding": False, "access_count": access_count, "half_life_s": half_life_s,
    }


def _hit(row: dict[str, Any], similarity: float = 0.7) -> float:
    svc = MemoryService(pool=None, provider=FakeModelProvider())  # _to_hit touches no pool
    return svc._to_hit({"row": row, "similarity": similarity, "retrievers": {"vector"}}, _NOW).score


def test_reactivation_lifts_a_reused_memory() -> None:
    base = _hit(_row(importance=0.5, access_count=0))
    reused = _hit(_row(importance=0.5, access_count=6))
    assert reused > base  # an often-recalled memory rises (§29)


def test_decayed_importance_lifts_a_more_important_memory() -> None:
    ordinary = _hit(_row(importance=0.4, access_count=0))
    important = _hit(_row(importance=0.95, access_count=0))
    assert important > ordinary  # importance is a ranking signal (§28/§37)


def test_relevance_still_dominates_importance() -> None:
    # An important-but-barely-relevant memory must not outrank a highly-relevant ordinary one (§38).
    important_irrelevant = _hit(_row(importance=1.0, access_count=8), similarity=0.15)
    relevant_ordinary = _hit(_row(importance=0.3, access_count=0), similarity=0.95)
    assert relevant_ordinary > important_irrelevant
