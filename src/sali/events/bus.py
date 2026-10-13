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
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def subscribe(self) -> asyncio.Event:
        """A wake-event that is set whenever ANY event is written. The subscriber waits on it (with its
        own interval as a fallback) and clears it before each tick."""
        waker = asyncio.Event()
        self._wakers.append(waker)
        return waker

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, _payload: str) -> None:
        for waker in self._wakers:
            waker.set()

    async def _install_listener(self) -> bool:
        """Acquire a pool conn and add the LISTEN handler. Returns True on success."""
        try:
            self._conn = await self._pool.acquire()
            await self._conn.add_listener(_CHANNEL, self._on_notify)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("event_bus_listen_install_failed", error=str(exc)[:200])
            self._conn = None
            return False

    async def start(self) -> None:
        self._stop.clear()
        if await self._install_listener():
            log.info("event_bus_listening", subscribers=len(self._wakers))
        else:
            log.warning("event bus could not start (falling back to polling)")
        # Supervisor: asyncpg's add_listener is bound to a specific connection and does NOT
        # self-heal after a Postgres restart or an idle-reaper close. Without this the bus goes
        # silently deaf, every waker.set() is lost, and all faculties silently fall back to
        # interval-only wake with no log. The supervisor probes the conn every 30s and
        # re-installs the listener on failure with exponential backoff.
        self._supervisor_task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30.0)
                return  # stop was set
            except TimeoutError:
                pass
            healthy = False
            if self._conn is not None:
                try:
                    await self._conn.fetchval("SELECT 1")
                    healthy = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("event_bus_listen_conn_dead", error=str(exc)[:200])
                    with contextlib.suppress(Exception):
                        await self._pool.release(self._conn)
                    self._conn = None
            if not healthy:
                await asyncio.sleep(min(backoff, 30.0))
                if await self._install_listener():
                    log.info("event_bus_reconnected", subscribers=len(self._wakers),
                             backoff_s=min(backoff, 30.0))
                    backoff = 1.0
                    # Wake all subscribers so they immediately catch up on missed events.
                    for waker in self._wakers:
                        waker.set()
                else:
                    backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop.set()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(BaseException):
                await self._supervisor_task
            self._supervisor_task = None
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
