"""Memory correctness · Increment 6 — retrieval traceability (§7).

Every retrieval records a trace of WHY each memory was selected — candidate count, which retriever found
it, and per-hit ranking signals + epistemic kind — available for diagnostics.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import _retrieval_trace

pytestmark = pytest.mark.db


async def test_trace_records_ranking_signals_and_contributions(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="the VPS runs Ubuntu 24.04",
                       source=MemorySource.SYSTEM_OBSERVATION)

    svc = RetrievalService(live_pool, FakeModelProvider())
    bundle = await svc.gather("what OS is on the VPS", classify("what OS is on the VPS"))
    trace = _retrieval_trace(bundle)

    assert trace["candidates"] >= 1
    assert isinstance(trace["by_retriever"], dict) and sum(trace["by_retriever"].values()) == trace["candidates"]
    top = trace["top"][0]
    # per-hit ranking signals are all present and typed
    assert set(top) >= {"retriever", "score", "confidence", "freshness", "stale", "knowledge_type"}
    assert top["retriever"] in ("vector", "keyword", "hybrid")
    assert top["knowledge_type"] == "observation"  # provenance carried into the trace


def test_empty_bundle_traces_cleanly() -> None:
    from sali.retrieval.models import RetrievalBundle

    trace = _retrieval_trace(RetrievalBundle())
    assert trace["candidates"] == 0 and trace["top"] == [] and trace["by_retriever"] == {}
