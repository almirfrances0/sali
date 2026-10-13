"""Freshness/decay and re-verification decisions (engineering rule 9). Spec test 6."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sali.core.enums import FreshnessPolicy as F
from sali.memory.decay import (
    ReverifyDecision,
    decayed_importance,
    decide_reverify,
    freshness_factor,
    is_stale,
)

NOW = datetime(2026, 1, 10, tzinfo=UTC)


def test_realtime_is_never_trusted_from_storage() -> None:
    assert freshness_factor(F.REALTIME, NOW, NOW) == 0.0
    assert is_stale(F.REALTIME, NOW, NOW)


def test_permanent_never_stale() -> None:
    old = NOW - timedelta(days=1000)
    assert freshness_factor(F.PERMANENT, old, NOW) == 1.0
    assert not is_stale(F.PERMANENT, old, NOW)


def test_within_window_fresh_then_declines() -> None:
    assert freshness_factor(F.DAILY, NOW - timedelta(hours=12), NOW) == 1.0
    declined = freshness_factor(F.DAILY, NOW - timedelta(days=3), NOW)
    assert 0.0 < declined < 1.0


def test_decay_reduces_but_never_deletes() -> None:
    hl = timedelta(days=30)
    one_half_life = decayed_importance(0.8, hl, timedelta(days=30))
    assert abs(one_half_life - 0.4) < 1e-6
    far = decayed_importance(0.8, hl, timedelta(days=3000))
    assert 0.0 < far < one_half_life  # sinks in ranking, never removed


def test_reverify_decisions() -> None:
    assert (
        decide_reverify(F.REALTIME, NOW, NOW, query_needs_live=False, has_inspector=False)
        is ReverifyDecision.INSPECT_LIVE
    )
    fresh = NOW - timedelta(hours=12)
    assert (
        decide_reverify(F.DAILY, fresh, NOW, query_needs_live=True, has_inspector=True)
        is ReverifyDecision.USE_STORED
    )
    stale = NOW - timedelta(days=5)
    assert (
        decide_reverify(F.DAILY, stale, NOW, query_needs_live=True, has_inspector=True)
        is ReverifyDecision.INSPECT_LIVE
    )
    assert (
        decide_reverify(F.DAILY, stale, NOW, query_needs_live=False, has_inspector=False)
        is ReverifyDecision.USE_STORED_LABELED_STALE
    )
