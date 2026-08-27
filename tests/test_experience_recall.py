"""Architecture review · Increment 2 — memory is GUARANTEED used on a task turn (§4/§6/point-10).

THE priority fix: a doing/fixing request routes to a task intent, pulls the prior procedure + past
incident layer-scoped, and they land in a dedicated context section BEFORE Sali acts — no recall-tool
call needed, and never double-surfaced.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.context.engine import ContextEngine
from sali.core.enums import MemoryLayer, MemorySource
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService

pytestmark = pytest.mark.db


def test_router_detects_task_intent() -> None:
    assert classify("how do I deploy project X?").use_experience is True
    assert classify("docker networking is broken again").use_experience is True
    assert classify("fix the failing build").use_experience is True
    assert classify("what did I say about the VPS?").use_experience is False  # a recall, not a task


async def test_procedure_and_incident_surface_on_a_task_turn(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.PROCEDURAL,
                       content="deploy project X: build, then ship, then verify",
                       source=MemorySource.INFERENCE, functional=True, claim_key="procedure:deployx",
                       structured={"steps": ["build", "ship", "verify"], "certainty": "learned"})
    await mem.remember(layer=MemoryLayer.EPISODIC,
                       content="deploying project X failed once because port 3000 was busy",
                       source=MemorySource.INFERENCE, functional=True, claim_key="failure:1",
                       structured={"kind": "incident"})

    svc = RetrievalService(live_pool, FakeModelProvider())
    q = "how do I deploy project X"
    bundle = await svc.gather(q, classify(q))

    assert any(h.memory.claim_key == "procedure:deployx" for h in bundle.procedures)
    assert any(h.memory.claim_key == "failure:1" for h in bundle.experiences)

    engine = ContextEngine(FakeModelProvider(), ctx_tokens=4096)
    result = engine.assemble(q, bundle, [])
    system = result.messages[0].content
    assert "experience" in result.included
    assert "build → ship → verify" in system            # the procedure steps, surfaced pre-act
    assert "port 3000 was busy" in system                # the past incident, surfaced pre-act


async def test_experience_is_not_double_surfaced(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.PROCEDURAL, content="restart the service the proven way",
                       source=MemorySource.INFERENCE, functional=True, claim_key="procedure:restart",
                       structured={"steps": ["stop", "start"], "certainty": "learned"})

    svc = RetrievalService(live_pool, FakeModelProvider())
    q = "how do I restart the service"
    bundle = await svc.gather(q, classify(q))
    proc_ids = {h.memory.claim_key for h in bundle.procedures}
    # the same memory must not appear in BOTH the procedures section and the generic memories section
    assert proc_ids and all(h.memory.claim_key not in proc_ids for h in bundle.memories)


async def test_non_task_turn_pulls_no_experience(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.PROCEDURAL, content="a procedure",
                       source=MemorySource.INFERENCE, functional=True, claim_key="procedure:x")
    svc = RetrievalService(live_pool, FakeModelProvider())
    q = "what is my VPS ip address"
    bundle = await svc.gather(q, classify(q))
    assert bundle.procedures == [] and bundle.experiences == []  # gated to task intent
