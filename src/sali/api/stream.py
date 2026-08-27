"""The event fan-out hub — the firehose, bridged to browsers (§14/§43).

Sali's whole nervous system already lands in ONE append-only table (`event`) announced on ONE Postgres
channel (`sali_events`, payload = new seq). Browsers cannot LISTEN Postgres directly and the notify
carries only the seq, so this hub is the single missing bridge: it holds ONE shared LISTEN connection
(via `EventBus`), and on every commit reads the new rows past a watermark, redacts each payload at the
boundary (§34), and fans them out to every connected client queue. One LISTEN for all viewers — not one
per browser. Slow clients drop frames (bounded queue) rather than stalling the pump (backpressure, §33).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sali.api.serialize import safe
from sali.events.bus import EventBus, wait_or_wake
from sali.obs.log import get_logger

log = get_logger("sali.api.stream")

_ROW_COLS = "seq, id, event_type, subject_type, subject_id, payload, created_at"


def frame(row: Any) -> dict[str, Any]:
    """One durable event → a redacted, JSON-safe wire frame. `payload` passes through `safe()` so no
    secret ever reaches a browser even though the row is stored unredacted at rest."""
    sid = row["subject_id"]
    return {
        "seq": int(row["seq"]),
        "id": str(row["id"]),
        "type": row["event_type"],
        "subject_type": row["subject_type"],
        "subject_id": str(sid) if sid is not None else None,
        "payload": safe(row["payload"] or {}),
        "at": row["created_at"].isoformat(),
    }


class EventHub:
    """Owns the single LISTEN connection + the pump task; fans new events out to registered client
    queues. Registration hands a browser its own bounded queue; the pump never blocks on a slow one."""

    def __init__(self, pool: Any, *, heartbeat: float = 5.0, client_buffer: int = 2000) -> None:
        self._pool = pool
        self._bus = EventBus(pool)
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._watermark = 0
        self._heartbeat = heartbeat
        self._client_buffer = client_buffer

    async def start(self) -> None:
        async with self._pool.acquire() as conn:
            # Start at HEAD: a fresh page loads history via the REST /api/events backfill, not by
            # replaying the entire log through the live pump on every process start.
            self._watermark = int(await conn.fetchval("SELECT COALESCE(max(seq), 0) FROM event") or 0)
        await self._bus.start()
        self._task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._bus.stop()

    def register(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._client_buffer)
        self._clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._clients.discard(q)

    async def backfill(self, since: int, *, limit: int = 500) -> list[dict[str, Any]]:
        """Redacted event rows a reconnecting client missed, oldest first (resume by its last seq)."""
        limit = max(1, min(limit, 2000))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_ROW_COLS} FROM event WHERE seq > $1 ORDER BY seq LIMIT $2", since, limit)
        return [frame(r) for r in rows]

    async def _pump(self) -> None:
        waker = self._bus.subscribe()
        while not self._stop.is_set():
            await wait_or_wake(self._stop, waker, self._heartbeat)
            if self._stop.is_set():
                break
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"SELECT {_ROW_COLS} FROM event WHERE seq > $1 ORDER BY seq", self._watermark)
            except Exception as exc:  # noqa: BLE001 - a read hiccup must not kill the pump
                log.warning("event_pump_read_failed", error=str(exc))
                continue
            for row in rows:
                self._watermark = int(row["seq"])
                f = frame(row)
                for q in list(self._clients):
                    # a slow browser drops frames; it recovers via REST backfill on its seq gap
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(f)
