"""Phase 2 · Increment 6 — subsystem health + online/offline awareness (§51/§52/§53).

Sali knows whether its own parts work and whether the internet is reachable, degrades honestly, and a
failed probe reads as down (never an exception). The system_health tool reports it.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.runtime.health import Health, HealthService, has_default_route
from sali.runtime.loop import _HealthSink
from sali.tools.builtins.self_tool import SystemHealth
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


def test_health_reports_degraded_subsystems() -> None:
    healthy = Health(datastore=True, model=True, embedder=True, perception=True, internet=True)
    assert healthy.all_ok and healthy.degraded == [] and "healthy" in healthy.render()

    degraded = Health(datastore=True, model=True, embedder=True, perception=False, internet=False)
    assert not degraded.all_ok
    assert degraded.degraded == ["perception", "internet"]
    assert "Degraded" in degraded.render() and "perception" in degraded.render()


def test_has_default_route_is_local_and_never_raises() -> None:
    assert isinstance(has_default_route(), bool)  # reads /proc/net/route, no packets, no exception


class _FailingProvider:
    async def health(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder is down")


async def test_a_failed_probe_reads_as_down_not_an_exception(live_pool: Any) -> None:
    svc = HealthService(live_pool, _FailingProvider(), perception=None)
    health = await svc.check()
    # datastore + model are up; embedder raised -> down (captured, not raised); perception absent -> down
    assert health.datastore is True and health.model is True
    assert health.embedder is False and "error" in health.detail["embedder"]
    assert health.perception is False


class _OkProvider:
    async def health(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3]]


async def test_system_health_tool_reports_through_the_sink(live_pool: Any) -> None:
    svc = HealthService(live_pool, _OkProvider(), perception=None)
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), health=_HealthSink(svc))

    res = await SystemHealth().run({}, ctx)
    assert res.ok
    assert res.output["subsystems"]["datastore"] is True
    assert res.output["subsystems"]["embedder"] is True
    assert "online" in res.output  # internet reachability is reported (§53)


async def test_health_tool_unavailable_without_sink() -> None:
    ctx = ToolContext(settings=Settings(), clock=SystemClock())
    res = await SystemHealth().run({}, ctx)
    assert not res.ok and "isn't available" in (res.error or "")
