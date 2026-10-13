"""A turn about Almir's life is not small talk.

Locks the fix for the 2026-09-07 06:02-06:06 transcript, where five messages in a row about how he
was feeling and what had happened to him were answered from machine telemetry with invented
specifics — culminating in "i lost 2 of my legs and 1 hand" being answered with a USB device
inventory. The cause was the small-talk latency clamp: such a message needs no tool, no graph and no
live host, so it satisfied every clause of the clamp, the optional tail was cut to 180 tokens, and
`about_almir`/`self`/four-of-five-memories were dropped while 2851 of 21504 tokens were in use.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sali.context.engine import ContextEngine
from sali.core.enums import FreshnessPolicy, MemoryLayer, MemorySource
from sali.core.ids import new_id
from sali.memory.models import Memory, MemoryHit
from sali.provider.fake import FakeModelProvider
from sali.retrieval.models import RetrievalBundle
from sali.runtime.loop import _is_personal_life

# Verbatim from the live transcript (sali.message seq 109-119), plus the same shapes he is likely
# to use next. Every one of these was answered from hardware state.
LIFE = [
    "real bad sali",
    "no im not good sali",
    "something else making me sad sali",
    "no sali, my wife left me\U0001F62D",
    "she’ll get back tomorrow? no we divorced sali?",
    "ooh sali i got an accident and i lost 2 of my legs and 1 hand\U0001F62D",
    "my mother is in hospital",
    "i lost my job",
    "im so lonely",
    "my dad passed away",
    "i feel terrible today",
]

# The clamp exists for a real reason — 28s vs 2.5s on a six-character message — so the detector has
# to leave genuine small talk and every technical turn alone.
NOT_LIFE = [
    "hey sali", "good morning", "how are you", "thanks sali", "good sali", "ok", "whats up",
    "my disk is full", "i lost my ssh key", "the gpu crashed", "my laptop broke",
    "build the site", "what is my hostname", "can you ping google", "my pc died last night",
    "check the logs", "how is the task going",
]


def test_life_messages_are_recognised() -> None:
    missed = [m for m in LIFE if not _is_personal_life(m)]
    assert not missed, f"would still be clamped to 180 tokens: {missed}"


def test_small_talk_and_machine_talk_are_not() -> None:
    wrong = [m for m in NOT_LIFE if _is_personal_life(m)]
    assert not wrong, f"would lose the latency clamp for nothing: {wrong}"


def test_a_technical_noun_wins() -> None:
    """The deny-list runs first: "my drive died" is work, however it is phrased."""
    assert not _is_personal_life("my drive died")
    assert not _is_personal_life("i lost my password\U0001F62D")


def test_a_long_paste_is_not_a_life_turn() -> None:
    assert not _is_personal_life("sad " * 300)


# ── the memory pronoun inversion ─────────────────────────────────────────────────────────────────

def _hit(content: str) -> MemoryHit:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mem = Memory(
        id=new_id(), layer=MemoryLayer.SEMANTIC, content=content,
        source=MemorySource.USER_EXPLICIT, confidence=0.8, importance=0.5, reliability=0.8,
        evidence_count=1, freshness=FreshnessPolicy.SLOW, valid_from=now, valid_until=None,
        last_verified=now, embed_status="done", structured={},
    )
    return MemoryHit(memory=mem, score=0.9, effective_confidence=0.8, freshness_factor=1.0,
                     stale=False, similarity=0.9, retriever="vector")


def _tail(content: str) -> str:
    engine = ContextEngine(FakeModelProvider(), ctx_tokens=4000, reserve=500)
    bundle = RetrievalBundle(memories=[_hit(content)])
    return "\n".join(m.content for m in engine.assemble("how am i doing", bundle, []).messages)


def test_a_second_person_memory_says_whose_you_it_is() -> None:
    """Capture stored Almir's "my wife left me" as "your wife left you". Rendered under a header
    addressed to Sali, that is Sali being told HIS wife left him."""
    tail = _tail("No sali, your wife left you")
    assert "written TO Almir" in tail
    assert "means HIM, not you" in tail


def test_a_memory_with_no_pronoun_pays_nothing() -> None:
    """The clause costs tail budget, so it is added only where it changes the reading."""
    tail = _tail("ping is not installed here")
    assert "written TO Almir" not in tail
    assert "What you remember" in tail


# ── the two bugs that made the first version of this fix inert ────────────────────────────────────

def test_an_intensifier_may_precede_the_negation() -> None:
    """"im really not good" was missed because the pattern only allowed the intensifier AFTER "not",
    so the live retest reproduced the original failure word for word."""
    for phrasing in ["im really not good today", "i'm honestly not okay",
                     "im not really good", "i am not doing great"]:
        assert _is_personal_life(phrasing), phrasing


def test_the_router_calls_a_life_turn_live_and_that_must_not_block_it() -> None:
    """The gate is overridden by `system_query`, never by `needs_live`.

    `_LIVE` is a list of temporal and quantitative phrases — "right now", "how much", "today" — so
    "im really not good today" classifies as intent="live" while having nothing to do with the host.
    Gating on `needs_live` therefore switched the whole fix off for any sentence containing "today",
    which is exactly how it was first written, and exactly how it first failed.
    """
    from sali.retrieval.router import classify
    plan = classify("no sali im really not good today")
    assert plan.needs_live, "the premise of this test changed — re-check the gate in loop.py"
    assert not plan.system_query
    assert _is_personal_life("no sali im really not good today")


def test_a_real_host_question_is_still_a_host_question() -> None:
    """The override that DOES apply: he is asking about the machine, so the machine note belongs."""
    from sali.retrieval.router import classify
    assert classify("how is my gpu doing").system_query
    assert not _is_personal_life("how is my gpu doing")
