"""EventBridge — connects PostgreSQL EventBus to live subscribers.

When any Sali process publishes an event:
1. PostgreSQL trigger fires NOTIFY on 'sali_events'
2. EventBus receives the notification (wake signal)
3. EventBridge fetches the durable event from PostgreSQL
4. EventBridge delivers it to live subscribers (WebSocket, terminal)

The NOTIFY is a wake-up signal, NOT the authoritative payload.
The PostgreSQL event table is always the source of truth.

Lifecycle:
- One EventBridge per API process
- Started at FastAPI startup
- Stopped at FastAPI shutdown
- No per-WebSocket-listener connections
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID

from sali.events.bus import EventBus, wait_or_wake
from sali.events.publisher import EventPublisher, SaliEvent
from sali.obs.log import get_logger

log = get_logger("sali.events.bridge")


class EventBridge:
    """Bridges PostgreSQL EventBus notifications to live subscribers.

    On each NOTIFY:
    1. Fetch the latest durable event from PostgreSQL
    2. Reconstruct a SaliEvent
    3. Deliver to local subscribers (WebSocket, terminal)
    """

    def __init__(self, pool: Any, publisher: EventPublisher) -> None:
        self._pool = pool
        self._publisher = publisher
        self._bus = EventBus(pool)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_seq: int = 0  # watermark for dedup
        self._local_seqs: set[int] = set()  # sequences published by this process

    def mark_local(self, seq: int) -> None:
        """Mark a sequence as published locally (skip in bridge delivery)."""
        self._local_seqs.add(seq)
        # Bound the set to prevent unbounded growth
        if len(self._local_seqs) > 1000:
            self._local_seqs = {s for s in self._local_seqs if s > self._last_seq - 500}

    async def start(self) -> None:
        """Start the bridge: subscribe to EventBus, begin listening."""
        self._stop.clear()
        # Watermark to the current tail so a RESTART never re-broadcasts the durable
        # event backlog as if it were live (that made old chats reappear one-by-one in
        # the app). Historical catch-up is the WS replay path (subscribe/after_seq),
        # which flags events replayed; the bridge only delivers events created AFTER
        # this process started.
        try:
            async with self._pool.acquire() as conn:
                tail = await conn.fetchval("SELECT coalesce(max(seq), 0) FROM event")
            self._last_seq = int(tail or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("event_bridge_watermark_failed", error=str(exc))
        # Subscribe to EventBus notifications
        self._wake = self._bus.subscribe()
        await self._bus.start()
        self._task = asyncio.create_task(self._listen_loop())
        log.info("event_bridge_started")

    async def stop(self) -> None:
        """Stop the bridge cleanly."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._bus.stop()
        log.info("event_bridge_stopped")

    async def _listen_loop(self) -> None:
        """Main loop: wait for EventBus wake OR heartbeat, fetch durable event, deliver to subscribers.

        Waits on BOTH stop and wake (via wait_or_wake) so a NOTIFY on 'sali_events' fires
        _fetch_and_deliver immediately instead of on the 5s polling tail. Previously this
        awaited only stop.wait — LISTEN plumbing worked but no consumer ever awaited the wake.
        """
        while not self._stop.is_set():
            await wait_or_wake(self._stop, self._wake, 5.0)
            if self._stop.is_set():
                break
            try:
                await self._fetch_and_deliver()
            except Exception as exc:  # noqa: BLE001
                log.warning("event_bridge_fetch_failed", error=str(exc))

    async def _fetch_and_deliver(self) -> None:
        """Fetch new durable events and deliver to local subscribers."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                "FROM event WHERE seq > $1 ORDER BY seq LIMIT 50",
                self._last_seq)

        for row in rows:
            seq = row["seq"]
            if seq <= self._last_seq:
                continue  # dedup
            if seq in self._local_seqs:
                self._local_seqs.discard(seq)
                self._last_seq = seq
                continue  # already delivered by local publisher

            # Reconstruct SaliEvent from durable row
            payload = dict(row["payload"]) if row["payload"] else {}
            event = SaliEvent(
                event_type=row["event_type"],
                event_id=payload.get("event_id", row["id"]),
                timestamp=row["created_at"],
                run_id=_parse_uuid(payload.get("run_id")),
                task_id=_parse_uuid(payload.get("task_id")),
                session_id=_parse_uuid(payload.get("session_id")),
                origin=payload.get("origin", "system"),
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                data={k: v for k, v in payload.items()
                      if k not in ("event_id", "run_id", "task_id", "session_id", "origin")},
                sequence=seq,
            )

            # Deliver to local subscribers only (not WebSocket — that's the publisher's job
            # for locally-published events. For cross-process events, we deliver to WebSocket here)
            await self._deliver_to_subscribers(event)
            self._last_seq = seq

    async def _deliver_to_subscribers(self, event: SaliEvent) -> None:
        """Deliver event to local subscribers (terminal, etc.) and WebSocket."""
        live_dict = event.to_live_dict()

        # WebSocket clients (cross-process delivery)
        if self._publisher._ws_manager is not None:
            with contextlib.suppress(Exception):
                await self._publisher._ws_manager.broadcast(live_dict)

        # Local subscribers
        for subscriber in self._publisher._subscribers:
            with contextlib.suppress(Exception):
                await subscriber.on_event(event)


def _parse_uuid(val: Any) -> UUID | None:
    """Parse a UUID from string, returning None if invalid."""
    if val is None:
        return None
    try:
        return UUID(str(val))
    except (ValueError, AttributeError):
        return None
