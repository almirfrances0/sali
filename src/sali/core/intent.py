"""Intent classification for what Almir just said (§10).

The pattern Almir named: understanding is intelligence, enforcement is deterministic. Today three
separate files answer the same question - "does Almir want to cancel the current work?" - in three
different vocabularies:

    * ``tasks/revocation.py:classify_revocation`` returns bool for durable abandonment.
    * ``tasks/authority.py:_detect_cancellation`` returns the matched phrase for the task authority.
    * ``runtime/attention.py:_BARE_CANCEL/_STOP_WORKING`` route the message for pre-emption.

Each catches a slightly different set of phrasings, and none catches the natural language space
completely. This module is the SEAM: one classification, wrapping the existing regex modules, so a
model-based fallback can be added without touching a single caller.

Two paths, tried in order:

    * FAST regex path - the existing regex modules, unchanged. Zero latency, zero risk of regressing
      the specific bugs Almir hit today (revocation of "leave it", the "dont forget it" negation
      guard, the shared cancellation detector). This handles the clear cases.
    * MODEL fallback - a tiny classification call, only when the fast path is inconclusive AND the
      message has cancellation-shaped structure (short, imperative). Feature-flagged OFF by default:
      the boundary exists, the code is measurable, but no per-turn latency is added until Almir opts
      in. Enable via ``settings.temporal.owner_timezone``-style config or programmatically.

The public interface is deliberately small. Callers ask ``understands_cancellation(message)`` and
receive an :class:`IntentSignal`. The signal carries source and confidence so audit + telemetry can
tell whether behaviour came from regex or model. Later work extends the classifier for correction,
resumption and other intents; this turn ships only cancellation because that is the miss with the
highest cost - Sali keeping working after Almir said stop is the exact failure §10 opens with.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IntentKind(StrEnum):
    """The categorised intent behind Almir's message.

    UNKNOWN is the honest answer when neither regex nor model could tell. Callers must treat it as
    "not a cancellation" rather than default-blocking, because a false positive here is destructive
    (an active task cancelled that Almir wanted continued) and a false negative is recoverable
    (Almir says stop again, more clearly)."""

    CANCEL = "cancel"
    CONTINUE = "continue"
    UNKNOWN = "unknown"


class IntentSource(StrEnum):
    REGEX = "regex"
    MODEL = "model"
    NONE = "none"


@dataclass(frozen=True)
class IntentSignal:
    """A classification with provenance.

    ``kind`` is what the classifier concluded. ``source`` says who concluded it, so telemetry and
    audit can attribute a decision to the fast path or the model path. ``confidence`` is 1.0 for
    regex hits (deterministic) and the model's own confidence otherwise; UNKNOWN carries 0.0.

    Note the deliberate absence of a ``phrase`` field: the caller does not need to know WHICH regex
    matched. If it does, the underlying module (``revocation``, ``authority``) is still callable
    directly. The abstraction keeps consumers thin."""

    kind: IntentKind
    source: IntentSource
    confidence: float = 1.0


class IntentClassifier:
    """The single seam for classifying Almir's intent.

    Constructed once (per runtime) and reused. Statelessness means it can be shared freely across
    tasks and turns. The model fallback is injected as a callable so tests never need to spin up an
    inference process."""

    def __init__(
        self,
        *,
        model_fallback: "IntentModelFallback | None" = None,
        model_fallback_enabled: bool = False,
    ) -> None:
        self._model = model_fallback
        # Off by default. Turning it on is a single config change; today the behaviour is 100% the
        # union of the existing regex modules.
        self._model_enabled = model_fallback_enabled and model_fallback is not None

    def understands_cancellation(self, message: str) -> IntentSignal:
        """Does Almir mean to stop / cancel / abandon the current work?

        The three regex modules already know that (revocation.classify_revocation, the shared
        authority detector consulted by attention.classify). Union them - a hit in any counts, all
        three refusing counts as "no cancel", and only then does the optional model fallback come
        into play."""
        if not message or len(message) > 4000:
            return IntentSignal(IntentKind.UNKNOWN, IntentSource.NONE, 0.0)
        if self._fast_cancel(message):
            return IntentSignal(IntentKind.CANCEL, IntentSource.REGEX, 1.0)
        # The fast path is definitively "no cancel" - a false negative here is recoverable (Almir can
        # say it more directly) and much cheaper than a false positive (destructive cancellation of
        # work he wanted). If the model fallback is enabled AND the message is control-shaped, ask.
        if self._model_enabled and self._looks_control_shaped(message):
            verdict = self._model.classify_cancellation(message) if self._model else None
            if verdict is not None:
                kind, conf = verdict
                return IntentSignal(kind, IntentSource.MODEL, conf)
        return IntentSignal(IntentKind.CONTINUE, IntentSource.REGEX, 1.0)

    @staticmethod
    def _fast_cancel(text: str) -> bool:
        """The union of the existing regex classifications, wrapped so callers see one predicate.

        Delegating rather than reimplementing preserves every fix that landed today: the
        _NEGATED guard against "don't forget it", the anchored bare forms ("stop.", "never mind"),
        the shared cancellation detector consulted by attention.classify. Any future refinement of
        the regexes automatically flows through."""
        # Local imports break a circular chain: revocation/authority already import each other in
        # some code paths, and importing them at module load time from here would deepen the graph.
        from sali.tasks.authority import _detect_cancellation
        from sali.tasks.revocation import classify_revocation

        if classify_revocation(text):
            return True
        if _detect_cancellation(text) is not None:
            return True
        return False

    @staticmethod
    def _looks_control_shaped(text: str) -> bool:
        """Cheap heuristic to decide whether the model fallback is worth invoking.

        Only messages that PLAUSIBLY control the current work get the model call. A long question
        about something else, a paragraph of instructions, an unrelated remark - none of these should
        wake an inference just to ask "is this a cancellation".

        Deliberately generous: false positives here mean an extra fast model call on a control-shaped
        message that turns out not to be a cancellation. False negatives mean the fallback never
        fires and we behave exactly as today. Neither is destructive."""
        stripped = text.strip()
        if not stripped or len(stripped) > 200:
            return False
        # Short, imperative-shaped, with cancellation-adjacent vocabulary. If none of these
        # trigger, the message is prose and the model call is not worth the round trip.
        lowered = stripped.lower()
        control_hints = (
            "stop", "cancel", "abandon", "drop", "forget", "leave", "never mind",
            "nevermind", "quit", "halt", "abort", "done with", "no more", "enough",
            "give up", "not anymore", "no longer",
        )
        return any(h in lowered for h in control_hints)


class IntentModelFallback:
    """The interface a model-backed fallback must implement.

    Kept as a Protocol-shaped ABC so a test can pass a fake with no coroutine loop. Real fallback
    lives in the runtime layer and calls a tiny (~200 ms) provider.chat with a one-word answer
    prompt. Deliberately NOT implemented in this module: the classifier must not depend on the
    provider or the loop."""

    def classify_cancellation(self, message: str) -> "tuple[IntentKind, float] | None":
        raise NotImplementedError


__all__ = [
    "IntentClassifier", "IntentKind", "IntentSignal", "IntentSource", "IntentModelFallback",
]
