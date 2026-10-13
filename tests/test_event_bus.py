"""Architecture review · Increment 8 — pg_notify event bus (§14): push instead of poll.

The 0017 trigger notifies on every event insert; EventBus LISTENs and wakes subscribers immediately, so
a consumer no longer waits its poll interval. wait_or_wake returns early on a new event, else heartbeats.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sali.events.bus import EventBus, wait_or_wake

pytestmark = pytest.mark.db


async def test_bus_wakes_a_subscriber_on_a_new_event(live_pool: Any) -> None:
    bus = EventBus(live_pool)
    waker = bus.subscribe()
    await bus.start()
    try:
        assert not waker.is_set()
        async with live_pool.acquire() as conn:  # any event insert fires the trigger → pg_notify
            await conn.execute(
                "INSERT INTO event (event_type, subject_type, payload) "
                "VALUES ('desktop.observed','desktop',$1)", {"summary": "x", "action": "record"})
        # the notify arrives shortly after commit; the subscriber is woken well within the poll interval
        await asyncio.wait_for(waker.wait(), timeout=3.0)
        assert waker.is_set()
    finally:
        await bus.stop()


async def test_wait_or_wake_returns_early_on_wake_and_clears_it() -> None:
    stop = asyncio.Event()
    wake = asyncio.Event()
    wake.set()  # a new event is pending
    # with a long timeout, it still returns immediately because wake is set...
    await asyncio.wait_for(wait_or_wake(stop, wake, timeout=30.0), timeout=1.0)
    assert not wake.is_set()  # ...and it clears the wake for the next tick


async def test_wait_or_wake_returns_on_stop() -> None:
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(wait_or_wake(stop, None, timeout=30.0), timeout=1.0)  # stop short-circuits


async def test_wait_or_wake_heartbeats_when_idle() -> None:
    stop = asyncio.Event()
    wake = asyncio.Event()
    # nothing set → it sleeps up to the (tiny) timeout, then returns (the fallback heartbeat)
    await asyncio.wait_for(wait_or_wake(stop, wake, timeout=0.05), timeout=1.0)
