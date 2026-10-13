"""Sali writes his own unprompted messages — and cannot make them worse than the template.

The wording of a self-initiated message moved from an f-string to the model. That is strictly better
when it works and strictly dangerous when it doesn't: a template cannot invent a deadline, cannot
arrive as three paragraphs, and cannot take the GPU while Almir is waiting. Every one of those is a
way generated prose could be worse than the fixed sentence it replaced, so every one is pinned here.

The invariant these all serve: the deterministic template is the FLOOR. Composition can only ever
improve on it, never degrade below it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from sali.cognitive import voice

FACTS = {"what he said he'd do": "email the signed tax docs to the accountant",
         "when it was due": "Sep 5 at 17:00",
         "overdue by": "1 day",
         "how many other things of his are also overdue": 2}
FALLBACK = "Heads up — you said you'd email the signed tax docs to the accountant, and it's overdue."
SITUATION = "Almir told you he would do something and the time he gave has passed."
GOOD = "You said you'd email the signed tax docs to the accountant — that was due Sep 5 at 17:00."


class _Says:
    def __init__(self, text: str) -> None:
        self.text = text

    async def chat(self, messages: Any, options: Any = None, preset: Any = None, **kw: Any) -> Any:
        return SimpleNamespace(content=self.text)


class _Down:
    async def chat(self, *a: Any, **kw: Any) -> Any:
        raise RuntimeError("ollama is down")


class _Hangs:
    async def chat(self, *a: Any, **kw: Any) -> Any:
        await asyncio.sleep(60)


async def _compose(provider: Any, coordinator: Any = None) -> str:
    voice.bind(provider=provider, coordinator=coordinator)
    try:
        return await voice.compose(situation=SITUATION, facts=FACTS, fallback=FALLBACK)
    finally:
        voice.bind(provider=None)   # never leak a bound provider into another test


async def test_his_own_words_ship_when_they_are_sound() -> None:
    assert await _compose(_Says(GOOD)) == GOOD


@pytest.mark.parametrize("invented, sentence", [
    # THE one that matters most: a wrong date about Almir's own commitment, in Sali's voice. This is
    # why the number check is exact-token and not a substring test — "Sep 9" passed a substring check
    # against facts containing "Sep 5 at 17:00" purely because a 9 appeared somewhere in the blob.
    ("a deadline", "You said you'd email the tax docs — that was due Sep 9 at 17:00."),
    ("a count", "You've got 14 things overdue now."),
    ("a path", "The docs are still sitting in /home/almir/taxes/2025.pdf."),
    ("a quote", 'You told me "I will do it tonight" and never did.'),
    ("a domain", "I could send it through mailgun.example.com for you."),
])
async def test_it_cannot_invent(invented: str, sentence: str) -> None:
    assert await _compose(_Says(sentence)) == FALLBACK, f"shipped an invented {invented}"


async def test_a_number_it_was_actually_given_is_not_fabrication() -> None:
    # The check must not be so strict that it always falls back — then nothing ever changes.
    assert await _compose(_Says("That one's 1 day overdue now.")) != FALLBACK


@pytest.mark.parametrize("shape, text", [
    ("empty", "   "),
    # push.py trims a banner at 240 chars, so an overlong message arrives on the phone as garbage
    # with the end cut off. Treat overrun as failure, never truncate.
    ("overlong", "word " * 200),
    ("markdown", "```\nreminder\n```"),
    ("tool syntax", '{"tool": "notify", "args": {}}'),
])
async def test_it_cannot_arrive_malformed(shape: str, text: str) -> None:
    assert await _compose(_Says(text)) == FALLBACK, f"shipped {shape}"


async def test_it_never_makes_almir_wait() -> None:
    # One model, one GPU, one machine-wide lock — and a raw generation is invisible to the
    # coordinator, so `_yield_background` cannot cancel it. Sali finding nicer words for himself
    # must never be the reason Almir's own turn is stuck behind the lease.
    busy = SimpleNamespace(foreground_demanded=True, is_thinking=True)
    assert await _compose(_Says(GOOD), busy) == FALLBACK


async def test_background_cognition_alone_does_not_silence_him() -> None:
    """ is the one cognition slot held by foreground OR BACKGROUND work, and background
    cognition runs constantly on an idle machine. Gating on it meant the composer declined nearly
    every time and Sali fell back to the fixed wording this module exists to replace — observed live
    as `voice_fallback why='almir is being served'` while he had been idle for 34 minutes.

    Only somebody WAITING counts. The 25s timeout covers a slot that never frees."""
    background = SimpleNamespace(foreground_demanded=False, is_thinking=True)
    assert await _compose(_Says(GOOD), background) == GOOD


async def test_it_composes_when_the_machine_is_idle() -> None:
    assert await _compose(
        _Says(GOOD), SimpleNamespace(foreground_demanded=False, is_thinking=False)) == GOOD


async def test_the_template_is_the_floor() -> None:
    assert await _compose(_Down()) == FALLBACK          # model down
    voice._TIMEOUT_S, original = 0.3, voice._TIMEOUT_S
    try:
        assert await _compose(_Hangs()) == FALLBACK     # model hangs — bounded, not forever
    finally:
        voice._TIMEOUT_S = original
    voice.bind(provider=None)
    assert await voice.compose(                          # unbound: tests, CLI probes, one-shots
        situation=SITUATION, facts=FACTS, fallback=FALLBACK) == FALLBACK
