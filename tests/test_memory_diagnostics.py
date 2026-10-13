"""Memory health diagnostics (§49): read-only aggregates over the memory subsystem."""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import diagnostics
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


async def test_health_reports_layer_counts_and_soft_spots(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="Project X uses PostgreSQL",
                       source=MemorySource.USER_EXPLICIT, importance=0.8)

    h = await diagnostics.health(live_pool)

    assert h["memory"]["current"] >= 1
    assert h["memory"]["by_layer"].get("semantic", 0) >= 1
    assert h["memory"]["embed_backlog"] >= 1  # just-added, not embedded yet — a real soft spot
    # every section is present and integer-typed (never crashes on an empty/young store)
    assert isinstance(h["graph"]["nodes"], int) and isinstance(h["graph"]["orphan_nodes"], int)
    assert isinstance(h["contradictions"]["total"], int) and isinstance(h["events"], int)
