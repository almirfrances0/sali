"""Observation sink (§17): attention gates, and an identical observation re-logged within the TTL is
debounced so a flapping port / re-saved file can't flood the durable event log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sali.events.base import EventKind, Observation
from sali.events.sink import DbObservationSink


class _Conn:
    def __init__(self, sink_inserts: list[Any]) -> None:
        self._inserts = sink_inserts

    async def execute(self, _sql: str, *args: Any) -> None:
        self._inserts.append(args)


class _FakePool:
    def __init__(self) -> None:
        self.inserts: list[Any] = []

    def acquire(self) -> Any:
        inserts = self.inserts

        class _Cm:
            async def __aenter__(self) -> _Conn:
                return _Conn(inserts)

            async def __aexit__(self, *_: Any) -> bool:
                return False

        return _Cm()


def _obs(summary: str, sample: str) -> Observation:
    now = datetime.now(UTC)
    return Observation(
        kind=EventKind.PORT_OPENED, summary=summary, importance=0.9, count=1,
        first_at=now, last_at=now, detail={"sample": sample},
    )


async def test_identical_observation_is_debounced() -> None:
    pool = _FakePool()
    sink = DbObservationSink(pool)
    await sink.observe(_obs("new listener", "tcp:0.0.0.0:8080"))
    await sink.observe(_obs("new listener", "tcp:0.0.0.0:8080"))  # identical, within TTL → suppressed
    await sink.observe(_obs("new listener", "tcp:0.0.0.0:9090"))  # different port → logged
    assert len(pool.inserts) == 2  # the duplicate was debounced, the two distinct ones logged
