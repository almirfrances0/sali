"""Memory correctness · Increment 4 — the memory_audit tool (§8).

Audits what Sali knows about a subject over the real stores: knowledge type, provenance + evidence
chain, verification status, superseded versions, and dependent procedures.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer
from sali.tools.builtins.memory_audit_tool import MemoryAudit
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


async def test_audit_reports_provenance_evidence_and_type(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        # an observed fact, corroborated twice (two evidence rows)
        await writer.remember(conn, layer=MemoryLayer.SEMANTIC, content="the GPU is an RTX 4070",
                              source=MemorySource.SYSTEM_OBSERVATION)
        await writer.remember(conn, layer=MemoryLayer.SEMANTIC, content="the GPU is an RTX 4070",
                              source=MemorySource.USER_EXPLICIT)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool)
    res = await MemoryAudit().run({"subject": "RTX 4070"}, ctx)
    assert res.ok and res.output["found"]
    k = res.output["knowledge"][0]
    assert k["knowledge_type"] == "observation" and k["epistemic_status"] == "I directly observed this"
    assert k["verification_status"] == "grounded"
    assert k["evidence_count"] >= 2 and len(k["evidence"]) >= 2  # the corroboration chain
    assert {"system_observation", "user_explicit"} <= {e["source"] for e in k["evidence"]}


async def test_audit_flags_a_belief_and_its_superseded_history(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        # a functional claim that changes — the old value is superseded, the new is a low-conf belief
        await writer.remember(conn, layer=MemoryLayer.SEMANTIC, content="the cache is Memcached",
                              source=MemorySource.INFERENCE, functional=True, claim_key="cache:type")
        await writer.remember(conn, layer=MemoryLayer.SEMANTIC, content="the cache is Redis",
                              source=MemorySource.USER_EXPLICIT, functional=True, claim_key="cache:type")

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool)
    res = await MemoryAudit().run({"subject": "the cache is"}, ctx)
    current = next(k for k in res.output["knowledge"] if "Redis" in k["content"])
    assert any("Memcached" in s["content"] for s in current["superseded_versions"])  # the history is visible


async def test_audit_of_the_unknown_says_so(live_pool: Any) -> None:
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=live_pool)
    res = await MemoryAudit().run({"subject": "quantum flux capacitor calibration"}, ctx)
    assert res.ok and res.output["found"] is False
    assert "no memory" in res.display
