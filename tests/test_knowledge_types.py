"""Memory correctness · Increment 3 — explicit knowledge types + epistemic status (§3/§12).

A memory's epistemic KIND is derived deterministically from its provenance/layer/grounding/confidence,
so Sali distinguishes "I observed this" from "I inferred this" from "I believe this but I'm not certain".
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.knowledge import KnowledgeType, classify_knowledge, epistemic_status


def test_direct_observation_is_an_observation() -> None:
    k = classify_knowledge(MemorySource.SYSTEM_OBSERVATION, MemoryLayer.SEMANTIC)
    assert k is KnowledgeType.OBSERVATION
    assert epistemic_status(k) == "I directly observed this"


def test_inference_is_an_inference() -> None:
    k = classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC, confidence=0.7)
    assert k is KnowledgeType.INFERENCE
    assert "inferred" in epistemic_status(k)


def test_ungrounded_or_low_confidence_is_a_belief() -> None:
    assert classify_knowledge(MemorySource.INFERENCE, MemoryLayer.SEMANTIC,
                              needs_grounding=True) is KnowledgeType.BELIEF
    assert classify_knowledge(MemorySource.CONVERSATION, MemoryLayer.SEMANTIC,
                              confidence=0.3) is KnowledgeType.BELIEF
    assert "not fully certain" in epistemic_status(KnowledgeType.BELIEF)


def test_layers_map_to_procedure_and_episode() -> None:
    assert classify_knowledge(MemorySource.INFERENCE, MemoryLayer.PROCEDURAL) is KnowledgeType.PROCEDURE
    assert classify_knowledge(MemorySource.INFERENCE, MemoryLayer.EPISODIC) is KnowledgeType.EPISODE


def test_a_confident_stated_fact_is_a_fact() -> None:
    k = classify_knowledge(MemorySource.USER_EXPLICIT, MemoryLayer.SEMANTIC, confidence=0.9)
    assert k is KnowledgeType.FACT and epistemic_status(k) == "I know this"


pytestmark = pytest.mark.db


async def test_recall_results_carry_the_knowledge_type(live_pool: Any) -> None:
    from sali.memory.service import MemoryService
    from sali.provider.fake import FakeModelProvider
    from sali.runtime.loop import _RecallSink

    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="the GPU is an RTX 4070",
                       source=MemorySource.SYSTEM_OBSERVATION)
    sink = _RecallSink(mem, None)
    hits = await sink.search("GPU RTX 4070")
    assert hits and hits[0]["knowledge_type"] == "observation"
    assert hits[0]["epistemic_status"] == "I directly observed this"
