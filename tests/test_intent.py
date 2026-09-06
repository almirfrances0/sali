"""The intent classifier is the single seam for "does Almir want to cancel the current work".

Almir on scattered classification: three files answered the same question in three different
vocabularies, each catching a different slice of natural language. This module consolidates them
without changing behaviour today, and opens a boundary a model-classifier can be dropped into later.

These tests pin two invariants:

    * The FAST path returns exactly the union of the three existing regex modules - a message that
      any of them would have caught still cancels, a message none of them would have caught still
      does not, and the negation guard from earlier today (the "don't forget it" fix) still holds.
    * The MODEL fallback works when enabled, and stays inert when disabled. Turning it on is one
      config change; the callers already delegate through IntentClassifier.
"""

from __future__ import annotations

import pytest

from sali.core.intent import (
    IntentClassifier, IntentKind, IntentModelFallback, IntentSignal, IntentSource,
)


class _ForcedCancelFallback(IntentModelFallback):
    """A test double that claims every message is a cancellation. Lets a test prove the fallback
    fires when enabled AND stays silent when disabled, without touching a model."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify_cancellation(self, message: str) -> tuple[IntentKind, float] | None:
        self.calls.append(message)
        return IntentKind.CANCEL, 0.85


# ── FAST PATH: identical to today's regex union ─────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Stop that.",
    "Forget it.",
    "Leave it.",
    "cancel that task",
    "forget about that project",
    "drop that idea",
    "scrap it",
    "abandon it",
    "never mind",
])
def test_the_regexes_that_used_to_catch_it_still_catch_it(message: str) -> None:
    """Every phrasing today's revocation + authority regexes recognise must remain a cancellation
    through the seam. A regression here is a caller who lost a fix."""
    signal = IntentClassifier().understands_cancellation(message)
    assert signal.kind is IntentKind.CANCEL, message
    assert signal.source is IntentSource.REGEX


@pytest.mark.parametrize("message", [
    "don't forget it",           # today's negation guard
    "dont forget that",
    "never forget that",
    "do not cancel that task",
    "can you leave it running in the background",
    "what did you stop doing yesterday?",
    "let me check on the archive script",
    "how are you today",
    "",
])
def test_the_negation_guard_and_ordinary_prose_are_not_cancellations(message: str) -> None:
    """The false-positive direction is the destructive one - an unwanted revocation cancels a task
    Almir wanted continued. These are the phrasings that were bugs earlier today; they must stay
    NOT cancellations here."""
    signal = IntentClassifier().understands_cancellation(message)
    assert signal.kind is not IntentKind.CANCEL, message


def test_a_message_beyond_the_size_cap_is_unknown_not_a_cancellation() -> None:
    """4000+ characters is a paste, not a control instruction. The classifier declines rather than
    scanning; the caller treats UNKNOWN as CONTINUE (safe direction)."""
    huge = "yes " * 2000
    signal = IntentClassifier().understands_cancellation(huge)
    assert signal.kind is IntentKind.UNKNOWN
    assert signal.source is IntentSource.NONE


# ── MODEL FALLBACK: opt-in, inert by default ─────────────────────────────────────────────────────

def test_the_model_fallback_stays_silent_when_disabled() -> None:
    """Zero regression today: the seam exists but is inert. Enabling it is a single boolean, and
    until Almir opts in, no per-turn latency is added and no model is asked."""
    fake = _ForcedCancelFallback()
    classifier = IntentClassifier(model_fallback=fake, model_fallback_enabled=False)
    # A control-shaped message the regexes DON'T catch (say, a phrasing not in any list yet):
    signal = classifier.understands_cancellation("actually done with this project")
    assert fake.calls == [], "fallback must NOT be called when disabled"
    assert signal.source is IntentSource.REGEX  # confident regex "no cancel"


def test_the_model_fallback_only_fires_on_control_shaped_messages() -> None:
    """The gate before the model call: only messages that plausibly control the current work are
    worth an inference. An unrelated question shouldn't wake the model."""
    fake = _ForcedCancelFallback()
    classifier = IntentClassifier(model_fallback=fake, model_fallback_enabled=True)

    classifier.understands_cancellation("what's the weather in Dar today")
    assert fake.calls == [], "no control-shaped vocabulary - fallback stays silent"

    classifier.understands_cancellation("actually done with this project now")
    assert len(fake.calls) == 1, "'done with' is control-shaped and the regexes miss it - fallback fires"


def test_the_model_verdict_wins_when_the_regex_was_inconclusive() -> None:
    """The whole point of the fallback: a phrasing the regexes miss ("done with this") gets caught
    by the model. The signal reports source=MODEL so telemetry can distinguish paths."""
    fake = _ForcedCancelFallback()
    classifier = IntentClassifier(model_fallback=fake, model_fallback_enabled=True)
    signal = classifier.understands_cancellation("actually done with this project")
    assert signal.kind is IntentKind.CANCEL
    assert signal.source is IntentSource.MODEL
    assert 0.0 < signal.confidence <= 1.0


def test_a_regex_cancel_never_calls_the_fallback() -> None:
    """The fast path wins when it fires. A definitive regex "yes cancel" is not second-guessed by
    the model - the fallback exists to catch MISSES, not to review clear hits."""
    fake = _ForcedCancelFallback()
    classifier = IntentClassifier(model_fallback=fake, model_fallback_enabled=True)
    signal = classifier.understands_cancellation("cancel that task please")
    assert signal.source is IntentSource.REGEX
    assert fake.calls == [], "fallback must not be called when the regex path is definitive"


# ── SEAM: the signal carries provenance ─────────────────────────────────────────────────────────

def test_the_signal_reports_where_the_verdict_came_from() -> None:
    """A future audit or telemetry line can attribute a cancellation to the fast path or the model
    path. That distinction is the point of the abstraction - without it, we know a task was
    cancelled but not why we thought Almir wanted it."""
    signal = IntentClassifier().understands_cancellation("stop that")
    assert isinstance(signal, IntentSignal)
    assert signal.source is IntentSource.REGEX
    assert signal.confidence == 1.0
