"""Sali can hear "you got that wrong", and a condolence is not a grovel.

Both from 2026-09-07. Twice Almir corrected Sali and Sali repeated the error in the next sentence
("Right, sorry Sir - the divorce. Yes, she's coming back tomorrow at 5 PM."), because nothing in the
prompt has ever told him a reply was wrong. And the apology-tic block flagged the three most natural
condolence openers, so in a sustained painful conversation the second compassionate reply was pushed
away from compassion.
"""
from __future__ import annotations

import pytest

from sali.context.engine import _PUSHBACK_RE, _apology_opener

PUSHBACK = [
    "is it wow or sad sali?",                       # register correction, verbatim
    "she'll get back tomorrow? no we divorced sali?",  # fact correction, verbatim
    "that's not what i said", "i didn't say that", "why did you say that",
    "you said 5 PM", "that's wrong", "no", "nope", "no im not good sali",
]
ORDINARY = [
    "hey sali", "thanks", "build the site", "how is the task going",
    "yes please do that", "ok sounds good", "what is my hostname",
]


@pytest.mark.parametrize("msg", PUSHBACK)
def test_pushback_is_recognised(msg: str) -> None:
    assert _PUSHBACK_RE.search(msg), msg


@pytest.mark.parametrize("msg", ORDINARY)
def test_ordinary_talk_is_not_pushback(msg: str) -> None:
    assert not _PUSHBACK_RE.search(msg), msg


@pytest.mark.parametrize("opener", [
    "I'm so sorry, Sir.",
    "I'm so sorry — that's awful.",
    "Sorry, Almir. That's devastating.",
])
def test_the_natural_condolences_are_still_seen_as_apologies(opener: str) -> None:
    """The detector itself is unchanged — these DO match. What changed is that a life turn no longer
    consults it, which is where the fix has to live: the phrasing cannot tell condolence from
    self-blame, only the situation can."""
    assert _apology_opener(opener)


def test_a_real_grovel_is_still_a_grovel() -> None:
    assert _apology_opener("Sorry, that was my mistake.")
