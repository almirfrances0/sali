"""Phase 1 · Increment 3 — the Attention Engine (§14/§15).

Deterministic tier + action decisions: routine churn is ignored, meaningful observations are recorded,
critical ones notify, and only a high-tier observation with an interpretation-needing signal wakes the
model (protecting the inference budget §79). The perception sink is now attention-driven.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sali.events.attention import AttentionAction, AttentionTier, assess
from sali.events.base import EventKind, Observation


def test_routine_churn_is_ignored() -> None:
    v = assess(importance=0.3)
    assert v.tier is AttentionTier.ROUTINE and v.action is AttentionAction.IGNORE


def test_meaningful_observation_is_recorded() -> None:
    v = assess(importance=0.6)
    assert v.tier is AttentionTier.INTERESTING and v.action is AttentionAction.RECORD


def test_a_big_burst_becomes_interesting_even_at_low_score() -> None:
    v = assess(importance=0.3, count=40)
    assert v.tier is AttentionTier.INTERESTING


def test_critical_signal_forces_notify() -> None:
    v = assess(importance=0.2, signals=frozenset({"disk_full"}))
    assert v.tier is AttentionTier.CRITICAL and v.action is AttentionAction.NOTIFY


def test_important_signal_needing_interpretation_wakes_reasoning() -> None:
    # a new listening service is important AND needs interpretation → investigate (wake the model)
    v = assess(importance=0.4, signals=frozenset({"new_service"}))
    assert v.tier is AttentionTier.IMPORTANT and v.action is AttentionAction.INVESTIGATE


def test_important_by_score_alone_records_without_waking_the_model() -> None:
    v = assess(importance=0.75)  # important, but no interpretation-needing signal
    assert v.tier is AttentionTier.IMPORTANT and v.action is AttentionAction.RECORD


# ---- the sink is attention-driven (DB) -------------------------------------------------------

pytestmark_db = pytest.mark.db


class _Acq:
    def __init__(self, c: Any) -> None:
        self.c = c

    async def __aenter__(self) -> Any:
        return self.c

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _Pool:
    def __init__(self, c: Any) -> None:
        self.c = c

    def acquire(self) -> Any:
        return _Acq(self.c)


@pytest.mark.db
async def test_sink_persists_with_tier_and_drops_routine(db_conn: Any) -> None:
    from sali.events.sink import DbObservationSink

    t = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
    sink = DbObservationSink(_Pool(db_conn))
    await sink.observe(Observation(EventKind.FILE_MODIFIED, "edited main.py", 0.75, 1, t, t))
    await sink.observe(Observation(EventKind.FILE_MODIFIED, "cache churn", 0.2, 1, t, t))  # routine

    rows = await db_conn.fetch(
        "SELECT payload FROM event WHERE event_type='desktop.observed' ORDER BY created_at")
    assert len(rows) == 1  # only the attention-worthy one
    assert rows[0]["payload"]["summary"] == "edited main.py"
    assert rows[0]["payload"]["tier"] == "important" and rows[0]["payload"]["action"] == "record"


@pytest.mark.db
async def test_sink_tags_a_source_delete_as_important(db_conn: Any) -> None:
    from sali.events.sink import DbObservationSink

    t = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
    sink = DbObservationSink(_Pool(db_conn))
    # a modest-importance delete of a source file is escalated by the 'source_delete' signal
    await sink.observe(Observation(EventKind.FILE_DELETED, "deleted app.py", 0.6, 1, t, t,
                                   detail={"sample": "/home/almir/proj/app.py"}))
    row = await db_conn.fetchrow("SELECT payload FROM event WHERE event_type='desktop.observed'")
    assert row["payload"]["tier"] == "important"
