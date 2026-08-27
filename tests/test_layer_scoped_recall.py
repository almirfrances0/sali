"""Architecture review · Increment 1 — layer-scoped retrieval (kills the top-24 blind spot).

A layer-scoped recall is a real SQL predicate, so a relevant procedural/incident memory that falls
outside the generic top-k is still found — the foundation for guaranteeing memory is USED (§4/§6).
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import retriever, writer
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


async def test_keyword_layer_filter_is_a_real_predicate(db_conn: Any) -> None:
    await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC, content="deploy notes for project X",
                          source=MemorySource.USER_EXPLICIT)
    await writer.remember(db_conn, layer=MemoryLayer.PROCEDURAL, content="deploy project X: build then ship",
                          source=MemorySource.INFERENCE, functional=True, claim_key="procedure:deployx")

    all_rows = await retriever.retrieve_keyword(db_conn, "deploy project X", 10)
    proc_rows = await retriever.retrieve_keyword(db_conn, "deploy project X", 10, layer="procedural")
    assert len(all_rows) == 2
    assert [r["layer"] for r in proc_rows] == ["procedural"]  # scoped to the layer in SQL


async def test_scoped_recall_finds_a_procedure_outside_the_generic_topk(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    # a wall of generic semantic memories that share the query terms — they crowd the generic top-k
    for i in range(30):
        await mem.remember(layer=MemoryLayer.SEMANTIC, content=f"note {i} about deploying the service",
                           source=MemorySource.CONVERSATION)
    await mem.remember(layer=MemoryLayer.PROCEDURAL, content="deploying the service: the proven steps",
                       source=MemorySource.INFERENCE, functional=True, claim_key="procedure:deploy")

    # generic recall (k=6) is dominated by the 30 semantic notes — the procedure may not appear
    procedural = await mem.retrieve("deploying the service", k=6, layer="procedural")
    assert procedural and any(h.memory.layer is MemoryLayer.PROCEDURAL for h in procedural)
    assert all(h.memory.layer is MemoryLayer.PROCEDURAL for h in procedural)  # nothing but procedures
