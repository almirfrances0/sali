"""Tests for the cognitive-cycle orchestration layer — the InitiativeDriver periodic tick +
DecisionTraceStore. Uses the shared `live_pool` fixture that TRUNCATEs the scratch DB per test.

The point of these tests is NOT to re-verify InitiativeEngine (already covered) — it's to
verify the missing wiring: (1) the driver actually calls generate_candidates() when unpaused,
(2) the resource gate defers correctly, (3) traces get written and the rollup aggregates them,
(4) the metrics endpoint returns the fields the /cognitive-metrics contract promises.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from sali.cognitive.decision_trace import VALID_MODES, DecisionTraceStore
from sali.cognitive.initiative_driver import InitiativeDriver


@pytest.mark.asyncio
async def test_decision_trace_record_and_rollup(live_pool) -> None:
    """Store round-trips: record 3 traces of different modes, rollup counts them by mode."""
    trace = DecisionTraceStore(live_pool)
    ids = []
    for mode in ("observe", "communicate", "defer"):
        tid = await trace.record(
            mode=mode, subject_ref=f"subj-{mode}",
            origin="test", reason_codes=["seed"], confidence=0.9,
            evidence={"k": "v"}, expected_outcome="ok")
        ids.append(tid)

    rollup = await trace.rollup()
    assert rollup["by_mode"]["observe"] == 1
    assert rollup["by_mode"]["communicate"] == 1
    assert rollup["by_mode"]["defer"] == 1
    assert rollup["outcomes"]["settled"] == 0
    assert rollup["outcomes"]["pending"] == 3

    await trace.observe_outcome(ids[0], actual_outcome="candidate_reviewed")
    rollup_after = await trace.rollup()
    assert rollup_after["outcomes"]["settled"] == 1
    assert rollup_after["outcomes"]["pending"] == 2


@pytest.mark.asyncio
async def test_decision_trace_rejects_unknown_mode(live_pool) -> None:
    trace = DecisionTraceStore(live_pool)
    with pytest.raises(ValueError):
        await trace.record(mode="teleport", origin="test")
    for m in VALID_MODES:
        # Sanity: every documented mode is accepted.
        await trace.record(mode=m, origin="test")


@pytest.mark.asyncio
async def test_initiative_driver_calls_generate_candidates(live_pool, monkeypatch) -> None:
    """The core wiring assertion: one tick invokes InitiativeEngine.generate_candidates and
    records observe traces for each returned id — this is the whole point of the driver."""
    from sali.runtime import initiative as init_mod

    fake_ids = ["init-a", "init-b", "init-c"]

    async def _fake_generate(self, now=None):  # noqa: ARG001
        return fake_ids

    async def _fake_next_wake(self, now=None):  # noqa: ARG001
        return datetime.now(UTC) + timedelta(seconds=1)  # due imminently

    monkeypatch.setattr(init_mod.InitiativeEngine, "generate_candidates", _fake_generate)
    monkeypatch.setattr(init_mod.InitiativeEngine, "next_wake", _fake_next_wake)

    driver = InitiativeDriver(live_pool, interval_s=60.0)
    touched = await driver.tick()

    assert touched == fake_ids
    assert driver._last_cycle_count == 3
    assert driver._last_cycle_at is not None

    trace = DecisionTraceStore(live_pool)
    recent = await trace.recent(limit=10)
    observe_rows = [r for r in recent if r["mode"] == "observe" and
                    r["origin"] == "initiative_driver"]
    assert len(observe_rows) == 3
    assert {r["subject_ref"] for r in observe_rows} == set(fake_ids)


@pytest.mark.asyncio
async def test_initiative_driver_resource_gate_defers(live_pool, monkeypatch) -> None:
    """Under simulated GPU pressure the driver should skip the scan and record a `defer`
    trace instead of firing generate_candidates."""
    from sali.runtime import initiative as init_mod

    called = {"n": 0}

    async def _fake_generate(self, now=None):  # noqa: ARG001
        called["n"] += 1
        return []

    monkeypatch.setattr(init_mod.InitiativeEngine, "generate_candidates", _fake_generate)

    # The REAL contract. This previously mocked {"gpu_vram_percent", "cpu_load_1m",
    # "disk_used_percent"} — a shape ResourceMonitor.snapshot() never returns (it returns
    # {"preserve", "reading", "state"}). So the gate read three keys that were always None,
    # every threshold compared against 0.0, and the branch was unreachable in production while
    # this test happily passed against the fiction. Assert the verdict the monitor actually emits.
    resources = MagicMock()
    resources.snapshot = AsyncMock(return_value={
        "state": "critical", "preserve": True, "reading": None})
    driver = InitiativeDriver(live_pool, resource_monitor=resources)
    touched = await driver.tick()

    assert touched == []
    assert called["n"] == 0

    trace = DecisionTraceStore(live_pool)
    recent = await trace.recent(limit=5)
    defer_rows = [r for r in recent if r["mode"] == "defer" and
                  r["origin"] == "initiative_driver"]
    assert len(defer_rows) == 1
    assert "resource_pressure" in defer_rows[0]["reason_codes"]


@pytest.mark.asyncio
async def test_initiative_driver_skips_when_nothing_due(live_pool, monkeypatch) -> None:
    """If next_wake is far in the future, the driver should NOT run the scanner — cheap skip."""
    from sali.runtime import initiative as init_mod

    called = {"n": 0}

    async def _fake_generate(self, now=None):  # noqa: ARG001
        called["n"] += 1
        return []

    async def _fake_next_wake(self, now=None, **kwargs):  # noqa: ARG001
        return datetime.now(UTC) + timedelta(hours=6)

    monkeypatch.setattr(init_mod.InitiativeEngine, "generate_candidates", _fake_generate)
    monkeypatch.setattr(init_mod.InitiativeEngine, "next_wake", _fake_next_wake)

    driver = InitiativeDriver(live_pool, interval_s=60.0)
    touched = await driver.tick()

    assert touched == []
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_driver_gate_ignores_its_own_curiosity_backoff(live_pool, monkeypatch) -> None:
    """A dispatched learning session must NOT switch the whole scanner off.

    _dispatch_curiosity reserves a topic by pushing that initiative row's `next_attempt` six hours
    out. next_wake() takes the MIN over routines/obligations/commitments AND initiative.next_attempt,
    and the driver skipped its whole cycle when that min was far away — so one curiosity dispatch
    blacked out every other source (commitments, obligations, goals, open loops) for six hours.
    Observed live before the fix: zero initiative cycles across six daemon restarts.

    The gate must therefore ask next_wake to EXCLUDE initiative back-offs. Per-topic pacing still
    happens, in the `next_attempt <= now()` predicate inside _dispatch_curiosity's own query."""
    from sali.runtime import initiative as init_mod

    called = {"n": 0}
    asked_with = {}

    async def _fake_generate(self, now=None):  # noqa: ARG001
        called["n"] += 1
        return []

    async def _fake_next_wake(self, now=None, include_initiative=True):  # noqa: ARG001
        asked_with["include_initiative"] = include_initiative
        # Nothing else is scheduled; the ONLY far-future wake is a curiosity back-off, which is
        # exactly what the gate must not honour.
        return (datetime.now(UTC) + timedelta(hours=6)) if include_initiative else None

    monkeypatch.setattr(init_mod.InitiativeEngine, "generate_candidates", _fake_generate)
    monkeypatch.setattr(init_mod.InitiativeEngine, "next_wake", _fake_next_wake)

    driver = InitiativeDriver(live_pool, interval_s=60.0)
    await driver.tick()

    assert asked_with["include_initiative"] is False, "the gate must exclude initiative back-offs"
    assert called["n"] == 1, "the scanner must still run after a curiosity was dispatched"


@pytest.mark.asyncio
async def test_next_wake_can_exclude_initiative_backoffs(live_pool) -> None:
    """The engine-level contract behind that gate: a pending initiative retry is visible to the
    general query (the status rollup wants it) and invisible to the driver's gate."""
    from sali.runtime.initiative import InitiativeEngine

    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO initiative (source, subject_ref, title, status, next_attempt) "
            "VALUES ('curiosity', 'probe-subject', 'probe', 'candidate', now() + interval '6 hours')")

    engine = InitiativeEngine(live_pool)
    assert await engine.next_wake() is not None, "the back-off is real and must stay visible"
    assert await engine.next_wake(include_initiative=False) is None, \
        "but the driver's gate must not see its own back-off"


@pytest.mark.asyncio
async def test_initiative_driver_run_terminates_on_stop(live_pool, monkeypatch) -> None:
    """The `run` faculty loop must exit cleanly when the stop event fires — otherwise it'd
    hang the daemon during shutdown."""
    from sali.runtime import initiative as init_mod

    async def _fake_generate(self, now=None):  # noqa: ARG001
        return []

    monkeypatch.setattr(init_mod.InitiativeEngine, "generate_candidates", _fake_generate)

    driver = InitiativeDriver(live_pool, interval_s=0.05)
    stop = asyncio.Event()

    async def _stopper():
        await asyncio.sleep(0.1)
        stop.set()

    await asyncio.gather(driver.run(stop), _stopper())
    # If we reach here without timeout, the loop terminated as expected.
