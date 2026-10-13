"""Epistemic typing (§7): classify_knowledge derives HOW Sali knows something, deterministically, and
the ASSUMPTION/HYPOTHESIS/UNKNOWN additions never regress the existing OBSERVATION/FACT/INFERENCE/BELIEF."""

from __future__ import annotations

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.knowledge import KnowledgeType, classify_knowledge, epistemic_status


def test_existing_types_unchanged() -> None:
    assert classify_knowledge(MemorySource.SYSTEM_OBSERVATION, MemoryLayer.SYSTEM_ENV) is KnowledgeType.OBSERVATION
    assert classify_knowledge(MemorySource.USER_EXPLICIT, MemoryLayer.SEMANTIC) is KnowledgeType.FACT
    assert classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC) is KnowledgeType.INFERENCE
    assert classify_knowledge(MemorySource.CONVERSATION, MemoryLayer.SEMANTIC, confidence=0.3) is KnowledgeType.BELIEF
    assert classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC, confidence=0.3) is KnowledgeType.BELIEF


def test_assumption_only_when_evidence_explicitly_zero() -> None:
    # A low-confidence inference with NO evidence reported is the weakest kind: an ASSUMPTION.
    assert (
        classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC, confidence=0.3, evidence_count=0)
        is KnowledgeType.ASSUMPTION
    )
    # With any backing evidence it is merely a (still-hedged) BELIEF, not a bare assumption.
    assert (
        classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC, confidence=0.3, evidence_count=2)
        is KnowledgeType.BELIEF
    )
    # Evidence unknown (None, the default) preserves the old behaviour — no ASSUMPTION regression.
    assert (
        classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC, confidence=0.3)
        is KnowledgeType.BELIEF
    )


def test_hypothesis_and_unknown_are_first_class() -> None:
    assert KnowledgeType.HYPOTHESIS.value == "hypothesis"
    assert KnowledgeType.UNKNOWN.value == "unknown"
    # Hedged kinds never get a redundant "(low confidence)" suffix — they already say they're uncertain.
    for k in (KnowledgeType.ASSUMPTION, KnowledgeType.HYPOTHESIS, KnowledgeType.UNKNOWN, KnowledgeType.BELIEF):
        assert "(low confidence)" not in epistemic_status(k, confidence=0.1)
    assert "don't know" in epistemic_status(KnowledgeType.UNKNOWN)
    # A confident-but-actually-low-confidence FACT still gets the honest hedge.
    assert "(low confidence)" in epistemic_status(KnowledgeType.FACT, confidence=0.1)
