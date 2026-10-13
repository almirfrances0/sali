"""The evidence hierarchy (Prompt 7 §3/§4/§5) — the deterministic core of adaptive learning.

Confidence is never invented by the model; it is DERIVED here from what actually happened: the level
of evidence behind a claim (0 model-assertion → 5 reviewer-verified) and the observation history
(how often it was seen, succeeded, failed). Promotion to durable, reusable knowledge is gated on this
— a single model assertion or a lone failure can never become truth (§5). Pure functions, no I/O, so
the rules are testable in isolation and identical everywhere they're applied.
"""

from __future__ import annotations

import hashlib
from enum import IntEnum


class EvidenceLevel(IntEnum):
    """How strong the evidence behind a claim is (§4). Higher outranks lower."""

    MODEL_ASSERTION = 0          # "npm install should work" — weakest
    EXTERNAL_OBSERVATION = 1     # a web source / a read-only observation
    TOOL_SUCCESS = 2             # a tool actually ran and succeeded once
    REPEATED_SUCCESS = 3         # the same thing succeeded across independent runs/tasks
    INDEPENDENT_VERIFICATION = 4 # verified by a separate check
    REVIEWER_VERIFIED = 5        # backed by a deterministic reviewer PASS — strongest


_BASE_CONFIDENCE: dict[int, float] = {
    0: 0.10, 1: 0.30, 2: 0.50, 3: 0.65, 4: 0.80, 5: 0.90,
}

# Promotion thresholds (§21/§22). A candidate becomes reusable knowledge only when the evidence clears
# one of these bars — never merely because the model proposed it.
_MIN_REPEATED_SUCCESSES = 3      # a positive lesson needs repeated independent success …
_MIN_NEGATIVE_FAILURES = 3       # … a negative lesson needs repeated failure + an explanation (§5)


def derive_confidence(
    *, evidence_level: int, times_successful: int = 0, times_failed: int = 0,
    times_observed: int = 1,
) -> float:
    """Confidence AS A FUNCTION of evidence — deterministic, monotone, bounded. The evidence level sets
    the base; the net direction of outcomes moves it up or down; repeated observation nudges it up a
    little. Never returns a value that isn't justified by the numbers passed in."""
    conf = _BASE_CONFIDENCE.get(int(evidence_level), 0.10)
    attempts = times_successful + times_failed
    if attempts:
        conf += 0.15 * (times_successful / attempts) - 0.15 * (times_failed / attempts)
    conf += 0.02 * min(max(times_observed, 1) - 1, 5)   # corroboration, capped
    return round(max(0.05, min(0.99, conf)), 3)


def is_negative(source_type: str | None) -> bool:
    """A negative lesson ('technique X fails under Y') — held to the failure-evidence bar (§5)."""
    return (source_type or "") in ("failure", "negative", "recurring_failure")


def is_promotable(
    *, evidence_level: int, verification_state: str, source_type: str | None = None,
    times_successful: int = 0, times_failed: int = 0, times_observed: int = 1,
    has_explanation: bool = False,
) -> bool:
    """Whether a candidate has earned promotion to durable, reusable knowledge (§12/§21/§22).

    Positive knowledge: a reviewer-verified PASS (level 5) is enough, OR repeated independent success
    (level ≥ 3 with ≥ _MIN_REPEATED_SUCCESSES successes and no failures). A negative lesson never
    auto-promotes from a single failure (§5): it needs repeated failure AND a verified explanation.
    A contradicted / rejected / superseded candidate is never promotable regardless of counts.
    """
    if verification_state in ("contradicted", "rejected", "superseded"):
        return False
    if is_negative(source_type):
        return times_failed >= _MIN_NEGATIVE_FAILURES and has_explanation
    if verification_state not in ("verified", "supported", "promoted"):
        return False
    if evidence_level >= EvidenceLevel.REVIEWER_VERIFIED:
        return True
    return (
        evidence_level >= EvidenceLevel.REPEATED_SUCCESS
        and times_successful >= _MIN_REPEATED_SUCCESSES
        and times_failed == 0
    )


def content_hash(lesson: str, scope: str, scope_ref: str | None = None) -> str:
    """A stable dedup key for a lesson in a scope (§13) — same lesson in the same scope hashes once, so
    re-observing it merges into the existing candidate rather than creating a duplicate."""
    norm = " ".join((lesson or "").lower().split())
    key = f"{scope}|{scope_ref or ''}|{norm}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
