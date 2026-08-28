"""Cross-turn retry policy (§3-6 / §9 / §38).

The loop's circuit breaker stops a SINGLE turn from spinning; this is the DURABLE, per-step decision
that survives turns and restarts. Given how many times a step has failed and WHY (its FailureClass from
Phase 0), it decides whether to retry (bounded, with backoff), block on a prerequisite, or escalate to
Almir. Pure — it reads the durable task_step columns and decides; it never loops and never auto-retries
a permission/fatal failure. Surfaced to the model via the continuation packet, so Sali chooses with
the reasoning in front of it rather than blindly re-running a failed action."""

from __future__ import annotations

from enum import StrEnum

from sali.core.errors import FailureClass

MAX_ATTEMPTS = 3


class RetryAction(StrEnum):
    RETRY = "retry"        # try again — as-is (transient) or after a fix (recoverable), bounded
    BLOCK = "block"        # a prerequisite isn't ready → wait on it, don't spin
    ESCALATE = "escalate"  # needs Almir / a privilege / a real fix → surface, never auto-retry


def retry_decision(
    attempts: int, failure_class: str | None, *, max_attempts: int = MAX_ATTEMPTS
) -> RetryAction:
    """Decide what to do about a failed step from its attempt count + FailureClass."""
    if failure_class in (FailureClass.PERMISSION.value, FailureClass.FATAL.value):
        return RetryAction.ESCALATE
    if failure_class == FailureClass.DEPENDENCY.value:
        return RetryAction.BLOCK
    if failure_class in (FailureClass.TRANSIENT.value, FailureClass.RECOVERABLE.value):
        return RetryAction.RETRY if attempts < max_attempts else RetryAction.ESCALATE
    # UNKNOWN / unclassified: one cautious retry, then escalate — never blindly loop (§38).
    return RetryAction.RETRY if attempts < 1 else RetryAction.ESCALATE


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff for a bounded transient retry (0.5s, 1s, 2s, 4s …), capped at 30s."""
    return min(30.0, 0.5 * (2.0 ** max(0, attempts - 1)))
