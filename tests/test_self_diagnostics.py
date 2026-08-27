"""Memory correctness · Increment 5 — the self_diagnostics tool (§9).

Reports subsystem health + concrete knowledge-consistency warnings derived from the real stores:
unresolved contradictions, a functional claim with two current values, and graph-vs-filesystem mismatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemorySource
from sali.graph.writer import ensure_node
from sali.tools.builtins.self_diagnostics_tool import SelfDiagnostics, consistency_warnings
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


async def test_clean_store_has_no_warnings(db_conn: Any) -> None:
    assert await consistency_warnings(db_conn) == []


async def test_open_contradiction_is_warned(db_conn: Any) -> None:
    from uuid import uuid4
    await db_conn.execute(
        "INSERT INTO contradiction (subject_type, old_id, new_id, status, old_source, new_source, "
        "  old_priority, new_priority) "
        "VALUES ('memory',$1,$2,'open','inference'::memory_source,'inference'::memory_source,20,20)",
        uuid4(), uuid4())
    warnings = await consistency_warnings(db_conn)
    assert any("unresolved contradiction" in w for w in warnings)


async def test_graph_vs_filesystem_mismatch_is_warned(db_conn: Any) -> None:
    await ensure_node(db_conn, node_type="location", name="ghost",
                      canonical_key="path:/definitely/not/here/xyz",
                      source=MemorySource.SYSTEM_OBSERVATION,
                      props={"path": "/definitely/not/here/xyz"})
    warnings = await consistency_warnings(db_conn)
    assert any("filesystem disagrees" in w for w in warnings)


async def test_conflicting_current_functional_claim_is_warned(db_conn: Any) -> None:
    # force the integrity fault: two CURRENT memories under one claim_key (should never happen normally)
    from uuid import uuid4
    for content in ("A", "B"):
        await db_conn.execute(
            "INSERT INTO memory (id, layer, content, source, claim_key, confidence, valid_from) "
            "VALUES ($1,'semantic'::memory_layer,$2,'inference'::memory_source,'dup:key',0.6,now())",
            uuid4(), content)
    warnings = await consistency_warnings(db_conn)
    assert any("conflicting CURRENT values" in w for w in warnings)


async def test_self_diagnostics_tool_runs(live_pool: Any) -> None:
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool)
    res = await SelfDiagnostics().run({}, ctx)
    assert res.ok and "metrics" in res.output and "warnings" in res.output
    assert "memories_current" in res.output["metrics"]
