"""Freshness and decay.

Freshness and truth are orthogonal. ``freshness_factor`` scales *how much a stored value
should be trusted right now*: realtime → 0 (never trust the stored value, inspect live),
within its window → 1, then a soft decline. ``decide_reverify`` turns that into an action.
Importance decays by half-life so stale-but-true facts sink in ranking — decay never
deletes (engineering rule 7/9).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import StrEnum

from sali.core.enums import FreshnessPolicy

# Mirrors the SQL `freshness_rule` table.
FRESHNESS_MAX_AGE: dict[FreshnessPolicy, timedelta] = {
    FreshnessPolicy.REALTIME: timedelta(0),
    FreshnessPolicy.FAST: timedelta(minutes=1),
    FreshnessPolicy.HOURLY: timedelta(hours=1),
    FreshnessPolicy.DAILY: timedelta(days=1),
    FreshnessPolicy.WEEKLY: timedelta(days=7),
    FreshnessPolicy.SLOW: timedelta(days=90),
    FreshnessPolicy.PERMANENT: timedelta(days=365_000),
}


class ReverifyDecision(StrEnum):
    INSPECT_LIVE = "inspect_live"
    USE_STORED = "use_stored"
    USE_STORED_LABELED_STALE = "use_stored_labeled_stale"


def freshness_factor(policy: FreshnessPolicy, last_verified: datetime, now: datetime) -> float:
    if policy is FreshnessPolicy.PERMANENT:
        return 1.0
    if policy is FreshnessPolicy.REALTIME:
        return 0.0  # never trust the stored value — inspect live
    max_age = FRESHNESS_MAX_AGE[policy]
    age = now - last_verified
    if age <= max_age:
        return 1.0
    over = (age - max_age).total_seconds()
    scale = max_age.total_seconds() or 1.0
    return max(0.25, math.exp(-over / scale))


def is_stale(policy: FreshnessPolicy, last_verified: datetime, now: datetime) -> bool:
    return freshness_factor(policy, last_verified, now) < 1.0


def effective_confidence(confidence: float, factor: float) -> float:
    return confidence * factor


def decayed_importance(importance: float, half_life: timedelta, elapsed: timedelta) -> float:
    """Half-life decay; monotonically non-increasing, never below zero."""
    hl = half_life.total_seconds() or 1.0
    factor = math.pow(0.5, elapsed.total_seconds() / hl)
    return max(0.0, importance * factor)


def decide_reverify(
    policy: FreshnessPolicy,
    last_verified: datetime,
    now: datetime,
    *,
    query_needs_live: bool,
    has_inspector: bool,
) -> ReverifyDecision:
    if policy is FreshnessPolicy.REALTIME:
        return ReverifyDecision.INSPECT_LIVE
    if not is_stale(policy, last_verified, now):
        return ReverifyDecision.USE_STORED
    if query_needs_live and has_inspector:
        return ReverifyDecision.INSPECT_LIVE
    return ReverifyDecision.USE_STORED_LABELED_STALE
