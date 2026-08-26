"""PerceptionEngine — wires the sources → filter → score → aggregate → sink, and keeps a bounded
buffer of recent observations (sali3 §7,11,42).

The decision steps (`ingest`, `drain`, `window_event`) are separated from the live `run` loop so the
whole pipeline is unit-testable with explicit timestamps and a fake sink — no real time, threads, or
watchdog in CI. `run` only adds the plumbing: a watchdog thread, a window-poll task, and a periodic
drain. The engine NEVER acts on an observation (§46) — it records and hands off to the sink.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sali.events.aggregate import Aggregator
from sali.events.base import DesktopEvent, EventKind, Observation, ObservationSink
from sali.events.fswatch import FsWatchSource
from sali.events.importance import is_noise, score
from sali.obs.log import get_logger

log = get_logger("sali.events.engine")

Snapshot = Callable[[], Awaitable[dict[str, Any]]]


class LoggingSink:
    """Default sink: log the observation and nothing else. A later fusion phase (§7) replaces this
    with a sink that feeds the twin/graph/memory."""

    async def observe(self, obs: Observation) -> None:
        log.info("observed", summary=obs.summary, importance=round(obs.importance, 2), count=obs.count)


class PerceptionEngine:
    def __init__(
        self, *, sink: ObservationSink | None = None, snapshot: Snapshot | None = None,
        fs_roots: list[str] | None = None, window_poll_s: float = 3.0,
        aggregate_window_s: float = 2.0, buffer_size: int = 200, queue_max: int = 10_000,
    ) -> None:
        self._sink: ObservationSink = sink or LoggingSink()
        self._snapshot = snapshot
        self._fs_roots = fs_roots or []
        self._window_poll_s = window_poll_s
        self._agg = Aggregator(window_s=aggregate_window_s)
        self._buffer: deque[Observation] = deque(maxlen=buffer_size)
        self._queue: asyncio.Queue[DesktopEvent] = asyncio.Queue(maxsize=queue_max)
        self._fs: FsWatchSource | None = None
        self._last_window: tuple[str, str] | None = None
        self.dropped = 0  # events shed under flood (visibility, never silent — §"no silent caps")

    # ── testable decision steps ──────────────────────────────────────────────

    def ingest(self, event: DesktopEvent) -> bool:
        """Filter + score + aggregate one event. Returns False if it was dropped as noise."""
        if is_noise(event):
            return False
        self._agg.add(event, score(event))
        return True

    async def drain(self, now: datetime) -> list[Observation]:
        """Surface any buckets that have gone quiet. Returns what was surfaced."""
        surfaced = self._agg.flush_due(now)
        for obs in surfaced:
            await self._surface(obs)
        return surfaced

    def window_event(self, snapshot: dict[str, Any] | None, now: datetime) -> DesktopEvent | None:
        """A WINDOW_FOCUS event iff the focused app/title changed since last seen; else None."""
        window = (snapshot or {}).get("window") or {}
        app = str(window.get("app") or "")
        title = str(window.get("title") or "")
        if not app:
            return None
        key = (app, title)
        if key == self._last_window:
            return None
        self._last_window = key
        return DesktopEvent(kind=EventKind.WINDOW_FOCUS, target=app, at=now,
                            source="window", extra={"title": title})

    def recent(self) -> list[Observation]:
        """The bounded buffer of recent observations — what a later query / the twin can read."""
        return list(self._buffer)

    async def _surface(self, obs: Observation) -> None:
        self._buffer.append(obs)
        try:
            await self._sink.observe(obs)
        except Exception as exc:  # noqa: BLE001 - a bad sink must not kill perception
            log.warning("observation sink failed: %s", exc)

    def _emit(self, event: DesktopEvent) -> None:
        # Filter BEFORE the bounded queue (§41 filter-first): otherwise a flood of churn (.git,
        # node_modules, a build) fills the queue and evicts the genuine edit interleaved in it. Runs
        # on the loop thread (fs events arrive via call_soon_threadsafe), and is_noise is pure.
        if is_noise(event):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1  # already the pathological flood case; shedding is the right move
            if self.dropped == 1 or self.dropped % 1000 == 0:  # visible, not a silent cap
                log.warning("shedding events under flood", dropped=self.dropped)

    # ── live loop ────────────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """Run continuously until `stop` is set. Starts the fs watcher + window poller, consumes and
        drains on a tick. Best-effort throughout — a source that can't start just doesn't contribute."""
        loop = asyncio.get_running_loop()
        if self._fs_roots:
            self._fs = FsWatchSource(self._fs_roots)
            if self._fs.start(loop, self._emit):
                log.info("watching", roots=self._fs_roots)
        tasks = [asyncio.create_task(self._consume_loop(stop))]
        if self._snapshot is not None:
            tasks.append(asyncio.create_task(self._window_loop(stop)))
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._fs is not None:
                self._fs.stop()
            for obs in self._agg.flush_all():  # surface whatever's still buffered on shutdown
                await self._surface(obs)

    async def _consume_loop(self, stop: asyncio.Event) -> None:
        # Floor the drain cadence so a 0 / tiny aggregate window can't turn wait_for(timeout=0) into a
        # 100%-CPU busy-spin on an idle desktop (the daemon's steady state).
        tick = max(0.05, min(self._agg.window_s, 1.0))
        while not stop.is_set():
            event: DesktopEvent | None = None
            with contextlib.suppress(asyncio.TimeoutError):
                event = await asyncio.wait_for(self._queue.get(), timeout=tick)
            if event is not None:
                self.ingest(event)
            await self.drain(datetime.now(UTC))

    async def _window_loop(self, stop: asyncio.Event) -> None:
        assert self._snapshot is not None
        while not stop.is_set():
            snap: dict[str, Any] | None = None
            try:
                snap = await self._snapshot()
            except Exception as exc:  # noqa: BLE001 - a perception hiccup never kills the loop
                log.warning("window snapshot failed: %s", exc)
            event = self.window_event(snap, datetime.now(UTC))
            if event is not None:
                self._emit(event)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._window_poll_s)
