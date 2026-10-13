"""A turn about Almir's life is sampled for the ordinary thing, not for novelty.

Live before: "i lost 2 of my legs and 1 hand" -> "Wow 😳 ... How old did you say you were? You're
barely twenty-two 🥲" (an invented age). After: "Two legs and one hand. That's heavy, Sir. I'm right
here though. How are you holding up?"
"""
from __future__ import annotations

from sali.provider.presets import CREATIVE, TENDER
from sali.runtime.loop import _LIFE_GROUNDING


def test_tender_samples_for_the_likely_thing() -> None:
    """After "i lost 2 of my legs", some form of "I'm so sorry" IS the most probable continuation.
    CREATIVE's temperature is what let the sampler reach past it for "Wow" — and reach for a fact
    Sali did not have."""
    assert TENDER.temperature < CREATIVE.temperature


def test_tender_does_not_tax_reflecting_his_words_back() -> None:
    """repeat_penalty charges the model for reusing tokens, Almir's own words included — and
    reflecting back what someone just told you is most of what listening sounds like."""
    assert TENDER.repeat_penalty < CREATIVE.repeat_penalty


def test_tender_is_not_deterministic() -> None:
    """Seed-pinned sampling produced the identical-reply-every-time behaviour an earlier audit had to
    unpick, and a canned sentence in answer to grief is its own failure."""
    assert TENDER.seed is None
    assert TENDER.temperature > 0


def test_the_life_note_gives_a_shape() -> None:
    """Q2_K follows structure where it ignores an attitude: first sentence about him, second says
    you are here, at most one question and only about HIM."""
    n = _LIFE_GROUNDING
    assert "FIRST sentence" in n and "SECOND sentence" in n
    assert "one short question" in n
    assert "never two" in n


def test_the_note_names_the_opener_almir_objected_to() -> None:
    """Abstract guidance did not land — the reply still opened "Wow" after a general rule against
    reacting to size. Naming the exact word did, and it is grounded in Almir's own correction rather
    than an invented style rule."""
    n = _LIFE_GROUNDING
    assert '"Wow"' in n
    assert "is it wow or sad sali?" in n


def test_the_one_question_is_about_him() -> None:
    """The question cap alone still produced "Which arm was it?" — a detail of the event."""
    assert "never a detail of the event" in _LIFE_GROUNDING
