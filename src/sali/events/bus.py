"""In-process event bus over the durable log (spec §14).

The `event` table is the durable spine; this makes it a PUSH bus. `EventBus` holds one dedicated
LISTEN connection on the 'sali_events' channel (fired by the 0017 trigger on every insert) and, on each
notification, wakes every subscriber so it polls from its own watermark immediately — converting
poll-interval latency into push, with the interval kept only as a fallback heartbeat. Consumers stay
watermark-based (the durable, replay-safe contract); the bus just tells them "there's something new".
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.events.bus")

_CHANNEL = "sali_events"


class EventBus:
    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._conn: Any = None
        self._wakers: list[asyncio.Event] = []

    def subscribe(self) -> asyncio.Event:
        """A wake-event that is set whenever ANY event is written. The subscriber waits on it (with its
        own interval as a fallback) and clears it before each tick."""
        waker = asyncio.Event()
        self._wakers.append(waker)
        return waker

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, _payload: str) -> None:
        for waker in self._wakers:
            waker.set()

    async def start(self) -> None:
        try:
            self._conn = await self._pool.acquire()
            await self._conn.add_listener(_CHANNEL, self._on_notify)
            log.info("event_bus_listening", subscribers=len(self._wakers))
        except Exception as exc:  # noqa: BLE001 - degrade to poll-only if LISTEN can't start
            log.warning("event bus could not start (falling back to polling): %s", exc)
            self._conn = None

    async def stop(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.remove_listener(_CHANNEL, self._on_notify)
                await self._pool.release(self._conn)
            self._conn = None


async def wait_or_wake(stop: asyncio.Event, wake: asyncio.Event | None, timeout: float) -> None:
    """Sleep up to `timeout`, but return early if `stop` is set or `wake` fires (a new event). Clears
    `wake` on return so the next tick starts fresh. Push when subscribed, heartbeat when not."""
    waiters = [asyncio.ensure_future(stop.wait())]
    if wake is not None:
        waiters.append(asyncio.ensure_future(wake.wait()))
    try:
        _, pending = await asyncio.wait(waiters, timeout=timeout,
                                        return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiters:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*waiters, return_exceptions=True)
    if wake is not None and wake.is_set():
        wake.clear()
