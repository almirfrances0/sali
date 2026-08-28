"""Cross-turn retry policy (§3-6/§9/§38): the durable per-step decision from attempts + FailureClass."""

from __future__ import annotations

from sali.tasks.retry import MAX_ATTEMPTS, RetryAction, backoff_seconds, retry_decision


def test_retry_decision_by_class() -> None:
    assert retry_decision(0, "transient") is RetryAction.RETRY
    assert retry_decision(0, "recoverable") is RetryAction.RETRY
    assert retry_decision(MAX_ATTEMPTS, "transient") is RetryAction.ESCALATE  # budget exhausted
    assert retry_decision(0, "permission") is RetryAction.ESCALATE  # never auto-retry
    assert retry_decision(0, "fatal") is RetryAction.ESCALATE
    assert retry_decision(0, "dependency") is RetryAction.BLOCK  # wait on a prerequisite
    # unknown: one cautious try, then escalate — never blindly loop
    assert retry_decision(0, "unknown") is RetryAction.RETRY
    assert retry_decision(1, "unknown") is RetryAction.ESCALATE
    assert retry_decision(0, None) is RetryAction.RETRY


def test_backoff_grows_and_caps() -> None:
    assert backoff_seconds(1) == 0.5
    assert backoff_seconds(2) == 1.0
    assert backoff_seconds(3) == 2.0
    assert backoff_seconds(50) == 30.0  # capped
