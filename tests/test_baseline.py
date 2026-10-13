"""Phase 3 · Increment 9 — persistent machine baseline + anomaly (§18/§19).

Sali learns the machine's normal (which ports are usually open), so a port that appears — even while
Sali was offline — is recognised as new, while first boot doesn't flag the whole machine as anomalous.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sali.events.base import EventKind
from sali.events.baseline import Baseline
from sali.events.syswatch import SystemState, SystemWatch

pytestmark = pytest.mark.db

_T = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


async def test_first_reconcile_establishes_baseline_then_flags_new(live_pool: Any) -> None:
    b = Baseline(live_pool)
    # first look — establishment, nothing is "new"
    assert await b.reconcile("port", {"tcp:22", "tcp:80"}) == set()
    assert await b.known("port") == {"tcp:22", "tcp:80"}
    # a genuinely new port later is flagged
    assert await b.reconcile("port", {"tcp:22", "tcp:80", "tcp:8080"}) == {"tcp:8080"}
    # a port that disappears is not "new" when it returns... it's already known
    assert await b.reconcile("port", {"tcp:22", "tcp:8080"}) == set()
    assert "tcp:8080" in await b.known("port")  # now part of normal


async def test_baseline_survives_a_restart(live_pool: Any) -> None:
    await Baseline(live_pool).reconcile("port", {"tcp:22"})  # established by one "process"
    # a brand-new Baseline (as after a restart) still knows the port from the datastore
    reborn = Baseline(live_pool)
    assert await reborn.reconcile("port", {"tcp:22", "tcp:9999"}) == {"tcp:9999"}  # only the truly new one


class _CollectSink:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def observe(self, obs: Any) -> None:
        self.seen.append(obs)


async def test_syswatch_with_baseline_flags_a_new_port(live_pool: Any) -> None:
    sink = _CollectSink()
    watch = SystemWatch(sink, baseline=Baseline(live_pool))
    states = [
        SystemState(ports={"tcp:22"}),                       # establish baseline
        SystemState(ports={"tcp:22", "tcp:8080"}),           # a new port appears
    ]

    async def _fake_probe() -> SystemState:
        return states.pop(0)

    watch.probe = _fake_probe  # type: ignore[method-assign]
    assert await watch.tick(_T) == []                        # baseline establishment, nothing surfaced
    delta = await watch.tick(_T)
    assert [o.kind for o in delta] == [EventKind.PORT_OPENED]
    assert delta[0].detail["sample"] == "tcp:8080"


async def test_syswatch_baseline_still_flags_failed_service(live_pool: Any) -> None:
    sink = _CollectSink()
    watch = SystemWatch(sink, baseline=Baseline(live_pool))
    states = [
        SystemState(ports={"tcp:22"}, failed_services=set()),
        SystemState(ports={"tcp:22"}, failed_services={"nginx.service"}),
    ]

    async def _fake_probe() -> SystemState:
        return states.pop(0)

    watch.probe = _fake_probe  # type: ignore[method-assign]
    await watch.tick(_T)
    delta = await watch.tick(_T)
    assert [o.kind for o in delta] == [EventKind.SERVICE_FAILED]  # failures stay always-notable
