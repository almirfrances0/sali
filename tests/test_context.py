"""Context assembly + token budgeting (spec test 13)."""

from __future__ import annotations

from datetime import UTC, datetime

from sali.context.engine import LIVE_NOTE, ContextEngine
from sali.core.enums import FreshnessPolicy, MemoryLayer, MemorySource
from sali.core.ids import new_id
from sali.memory.models import Memory, MemoryHit
from sali.provider.fake import FakeModelProvider
from sali.retrieval.models import RetrievalBundle


def _hit(content: str, *, stale: bool = False, score: float = 0.5) -> MemoryHit:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mem = Memory(
        id=new_id(), layer=MemoryLayer.SEMANTIC, content=content,
        source=MemorySource.USER_EXPLICIT, confidence=0.8, importance=0.5, reliability=0.8,
        evidence_count=1, freshness=FreshnessPolicy.SLOW, valid_from=now, valid_until=None,
        last_verified=now, embed_status="done", structured={},
    )
    return MemoryHit(
        memory=mem, score=score, effective_confidence=0.8 if not stale else 0.2,
        freshness_factor=1.0 if not stale else 0.2, stale=stale, similarity=score, retriever="vector",
    )


def _engine(ctx: int = 1500, reserve: int = 500) -> ContextEngine:
    return ContextEngine(FakeModelProvider(), ctx_tokens=ctx, reserve=reserve)


def test_budget_is_respected_and_p0_never_cut() -> None:
    # A flood of large memories must never evict identity/security, and never exceed budget.
    big = "x " * 400  # ~800 chars → well over a small budget on its own
    bundle = RetrievalBundle(memories=[_hit(big + str(i), score=0.5) for i in range(20)])
    engine = _engine(ctx=1500, reserve=500)  # budget = 1000
    assembled = engine.assemble("what do you know?", bundle, tool_specs=[])

    assert "identity" in assembled.included and "security" in assembled.included
    assert assembled.dropped  # something had to be dropped
    assert assembled.est_tokens <= engine.budget  # never exceeds the budget
    # identity + security text is actually present in the rendered system message
    system = assembled.messages[0].content
    assert "You are Sali" in system
    assert "genuinely destructive" in system  # the security/judgment note (P0) survived


def test_stale_memory_is_surfaced_not_asserted() -> None:
    bundle = RetrievalBundle(memories=[_hit("GPU driver is 550", stale=True)])
    assembled = _engine().assemble("what driver do I have?", bundle, tool_specs=[])
    assert "GPU driver is 550" in assembled.conflicts
    system = assembled.messages[0].content
    assert "STALE" in system  # labeled, with an instruction to verify


def test_live_note_included_when_present() -> None:
    assembled = _engine().assemble(
        "how much RAM now?", RetrievalBundle(), tool_specs=[], live_note=LIVE_NOTE
    )
    assert "live" in assembled.included
    assert "go check it directly" in assembled.messages[0].content.lower()


def test_user_query_always_in_messages() -> None:
    assembled = _engine().assemble("a very specific question", RetrievalBundle(), tool_specs=[])
    assert assembled.messages[-1].role == "user"
    assert assembled.messages[-1].content == "a very specific question"
