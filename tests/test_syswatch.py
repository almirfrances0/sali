"""Phase 1 · Increment 4 — system-state event source feeding attention (§13/§78).

The machine's own changes — a new listening port, a failed service, disk pressure — become
Observations the Attention Engine tiers (new service → investigate; failed service / full disk →
notify). Deterministic diff; the first poll is only a baseline; parsing is robust.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sali.events.attention import AttentionAction, assess
from sali.events.base import EventKind
from sali.events.sink import _signals_for
from sali.events.syswatch import (
    SystemState,
    SystemWatch,
    _parse_disk,
    _parse_failed,
    _parse_ports,
)

_T = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def test_parsers_are_robust() -> None:
    assert "tcp:0.0.0.0:8080" in _parse_ports("Netid State Recv Send Local Peer\ntcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*")
    assert _parse_failed("nginx.service loaded failed failed nginx") == {"nginx.service"}
    assert _parse_disk("Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/sda1 100 95 5 95% /") == {"/"}
    assert _parse_ports("") == set() and _parse_failed("") == set()  # empty → empty, never raises


class _CollectSink:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def observe(self, obs: Any) -> None:
        self.seen.append(obs)


async def test_first_tick_is_baseline_then_emits_deltas() -> None:
    sink = _CollectSink()
    watch = SystemWatch(sink)
    states = [
        SystemState(ports={"tcp:0.0.0.0:22"}),                                # baseline
        SystemState(ports={"tcp:0.0.0.0:22", "tcp:0.0.0.0:8080"}),           # a new port appears
    ]

    async def _fake_probe() -> SystemState:
        return states.pop(0)

    watch.probe = _fake_probe  # type: ignore[method-assign]
    assert await watch.tick(_T) == []                     # first poll = baseline, emits nothing
    delta = await watch.tick(_T)
    assert [o.kind for o in delta] == [EventKind.PORT_OPENED]  # only the genuinely new port
    assert len(sink.seen) == 1                            # and it reached the sink


def test_diff_reports_all_three_kinds_of_change() -> None:
    watch = SystemWatch(_CollectSink())
    before = SystemState(ports={"tcp:0.0.0.0:22"})
    after = SystemState(ports={"tcp:0.0.0.0:22", "tcp:0.0.0.0:8080"},
                        failed_services={"nginx.service"}, disk_pressure={"/"})
    assert {o.kind for o in watch.diff(before, after, _T)} == {
        EventKind.PORT_OPENED, EventKind.SERVICE_FAILED, EventKind.DISK_PRESSURE}


def test_system_observations_tier_correctly_through_attention() -> None:
    watch = SystemWatch(_CollectSink())
    before = SystemState()
    after = SystemState(ports={"tcp:0.0.0.0:8080"}, failed_services={"nginx.service"},
                        disk_pressure={"/"})
    obs = {o.kind: o for o in watch.diff(before, after, _T)}

    # a new listening service → important + needs interpretation → investigate (wake the model)
    port_v = assess(importance=obs[EventKind.PORT_OPENED].importance,
                    signals=_signals_for(obs[EventKind.PORT_OPENED]))
    assert port_v.action is AttentionAction.INVESTIGATE
    # a failed service and disk pressure → critical → notify Almir
    for kind in (EventKind.SERVICE_FAILED, EventKind.DISK_PRESSURE):
        v = assess(importance=obs[kind].importance, signals=_signals_for(obs[kind]))
        assert v.action is AttentionAction.NOTIFY


@pytest.mark.db
async def test_syswatch_persists_a_failed_service_observation(db_conn: Any) -> None:
    from sali.events.sink import DbObservationSink

    class _Pool:
        def acquire(self) -> Any:
            conn = db_conn

            class _Acq:
                async def __aenter__(self) -> Any:
                    return conn

                async def __aexit__(self, *a: Any) -> bool:
                    return False
            return _Acq()

    watch = SystemWatch(DbObservationSink(_Pool()))
    # emit via diff→sink directly (no live probe in CI)
    for o in watch.diff(SystemState(), SystemState(failed_services={"nginx.service"}), _T):
        await watch._sink.observe(o)

    row = await db_conn.fetchrow(
        "SELECT payload FROM event WHERE event_type='desktop.observed' "
        "AND payload->>'kind'='service_failed'")
    assert row is not None and row["payload"]["action"] == "notify"
