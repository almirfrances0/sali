"""Long-running context continuity (Prompt 3) — unit proofs.

The governing principle: the LLM context is DISPOSABLE, the task state is DURABLE. A context-window
boundary must never be read as task completion. These tests pin the deterministic pieces that make
that true: the provider-derived token budget and its safe→emergency status ladder, context-overflow
detection, the evidence-typed continuation packet (verified vs merely attempted — no hallucinated
completion), and that folding stays bounded no matter how many times it happens (no packet-in-packet).
"""

from __future__ import annotations

from uuid import uuid4

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.errors import ProviderError
from sali.provider.base import ChatMessage
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime import context_budget, continuation
from sali.runtime.context_budget import ContextStatus
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tasks.models import Task, TaskStep
from sali.tools.registry import default_registry


def _msgs(n: int, chars: int) -> list[ChatMessage]:
    return [ChatMessage(role="user", content="x" * chars) for _ in range(n)]


def _loop(provider: FakeModelProvider) -> AgentLoop:
    # pool=None: _fold_messages tolerates a pool-less loop (the deterministic task header is simply
    # omitted), so we can exercise folding without a database.
    return AgentLoop(
        pool=None, provider=provider, retrieval=RetrievalService(None, provider),
        context=ContextEngine(provider, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))


# ── budget: measurement + the status ladder (§7/§27) ───────────────────────────────────────────────

def test_estimate_is_a_conservative_upper_bound() -> None:
    p = FakeModelProvider()
    est = context_budget.estimate_tokens(p, [ChatMessage(role="user", content="a" * 400)])
    assert est >= p.count_tokens("a" * 400)  # padded above the raw heuristic, never below it


def test_status_ladder_escalates_with_load() -> None:
    p = FakeModelProvider()
    small = context_budget.assess(p, _msgs(1, 40), limit=10_000, reserved_output=0)
    assert small.status is ContextStatus.SAFE and not small.should_compact and not small.is_approaching

    huge = context_budget.assess(p, _msgs(50, 4000), limit=10_000, reserved_output=0)
    assert huge.status is ContextStatus.EMERGENCY and huge.should_compact and huge.is_emergency

    # ~0.87 of a tiny window → an intermediate, "getting full" band that still says keep going
    mid = context_budget.assess(p, _msgs(1, 4000), limit=1500, reserved_output=0)
    assert mid.is_approaching and mid.status in (
        ContextStatus.APPROACHING_LIMIT, ContextStatus.COMPACT_REQUIRED)


def test_compact_fires_before_the_hard_wall() -> None:
    # COMPACT_REQUIRED must begin strictly BELOW the limit (proactive), not at/after it.
    p = FakeModelProvider()
    b = context_budget.assess(p, _msgs(1, 2700), limit=1000, reserved_output=0)  # ~0.89 of usable
    assert b.should_compact and not b.is_emergency  # compact-before-overflow, room still remained


def test_reserved_output_shrinks_the_usable_budget() -> None:
    p = FakeModelProvider()
    b = context_budget.assess(p, _msgs(1, 40), limit=5000, reserved_output=2048)
    assert b.usable == 5000 - 2048
    assert b.remaining == b.usable - b.used
    assert b.limit == 5000 and b.as_dict()["status"] == b.status.value


def test_resolve_limit_caps_at_the_operational_budget() -> None:
    # Prompt 6 §3: the effective limit is min(physical model window, Sali's operational context budget).
    s = Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))
    assert s.runtime.context_limit == 24_576  # the small working set, not the model's real window
    # a provider window SMALLER than the operational budget is the ceiling
    assert context_budget.resolve_limit(FakeModelProvider(ctx_limit=4096), s) == 4096
    # a physical window LARGER than the operational budget is capped DOWN to the operational budget
    assert context_budget.resolve_limit(FakeModelProvider(), s) == s.runtime.context_limit
    assert context_budget.resolve_limit(FakeModelProvider(ctx_limit=131_072), s) == s.runtime.context_limit
    # no provider limit AND no settings → an explicit, observable fallback, still bounded
    assert context_budget.resolve_limit(FakeModelProvider(), None) == context_budget._FALLBACK_LIMIT


def test_is_context_overflow_detects_provider_errors_precisely() -> None:
    assert context_budget.is_context_overflow(
        ProviderError("ollama chat failed: 500 - context length exceeded"))
    assert context_budget.is_context_overflow("input is too large for the context window")
    assert context_budget.is_context_overflow("num_ctx is too small; prompt is too long")
    # unrelated failures are NOT mistaken for overflow (so we don't compact when we shouldn't)
    assert not context_budget.is_context_overflow(ProviderError("connection refused"))
    assert not context_budget.is_context_overflow("model stream ended without a result")


# ── continuation packet: deterministic evidence typing (§25/§26) ───────────────────────────────────

def _task() -> Task:
    return Task(
        id=uuid4(), objective="Build the Laravel app", status="running",
        steps=[
            TaskStep(seq=1, description="scaffold", status="done", verified=True),
            TaskStep(seq=2, description="wire auth", status="done", verified=False),  # attempted, unproven
            TaskStep(seq=3, description="migrate", status="failed", last_error="db down",
                     failure_class="transient", attempts=1),
            TaskStep(seq=4, description="deploy", status="pending"),
        ])


def test_packet_grades_each_step_by_durable_evidence() -> None:
    tf = continuation.task_fields(_task())
    grades = {s["seq"]: s["evidence"] for s in tf["steps"]}
    assert grades == {1: "verified", 2: "attempted", 3: "failed", 4: "pending"}
    assert tf["verification"] == {
        "verified": 1, "attempted": 1, "failed": 1, "pending": 1, "unknown": 0}


def test_header_never_renders_an_unverified_step_as_proven() -> None:
    header = continuation.render_task_header(_task())
    assert "Build the Laravel app" in header
    # only the truly-verified step counts as proven; a done-but-unverified step is shown as such
    assert "VERIFIED: 1 proven" in header and "1 done-unverified" in header
    # the failed step 3 is NOT "next" (it's not pending) — the deterministic next ready step is 4
    assert "NEXT: step 4" in header and "FAILED step 3" in header


def test_packet_roundtrip_preserves_evidence_and_stays_deterministic() -> None:
    packet = continuation.build_packet(_task(), "DONE: scaffolded.\nNEXT: run migrations.")
    assert packet["task"]["verification"]["verified"] == 1
    rendered = continuation.render_packet(packet)
    assert "Build the Laravel app" in rendered and "VERIFIED: 1 proven" in rendered


def test_packet_of_no_task_is_empty() -> None:
    assert continuation.task_fields(None) == {}
    assert continuation.render_task_header(None) == ""


# ── folding stays bounded: no packet-in-packet accumulation (§10/§27) ───────────────────────────────

async def test_repeated_folding_never_grows_and_stays_small() -> None:
    loop = _loop(FakeModelProvider())  # each fold's summary is a short "(fake response)"
    msgs = [ChatMessage(role="system", content="S"), ChatMessage(role="user", content="Build the app")]
    msgs += [ChatMessage(role="assistant", content="work " * 300) for _ in range(8)]

    for _ in range(6):  # many consecutive folds — the failure mode we're guarding against
        folded = await loop._fold_messages(msgs)
        # A fold collapses to: system + first user + one progress note (+ at most one recent result).
        assert len(folded) <= 4
        # The carried context is a bounded ceiling, not an ever-growing nest of prior packets.
        assert context_budget.estimate_tokens(loop.provider, folded) < 2500
        # the turn "continues": more work piles on, then we fold again
        msgs = folded + [ChatMessage(role="assistant", content="more " * 300) for _ in range(8)]

    assert len(await loop._fold_messages(msgs)) <= 4
