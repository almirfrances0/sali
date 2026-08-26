"""Event-driven desktop observation (spec §16).

    Linux state  →  observe (cheap, deterministic)  →  diff = importance detection
                 →  ignore (nothing changed) / store (emit change events + refresh memory)

The daemon just loops :meth:`TwinService.refresh` on an interval. The expensive part — the LLM —
is deliberately NOT in this loop: importance detection is the deterministic diff already done by
``sync`` (only added/removed entities become events; an unchanged cycle corroborates and embeds
nothing). So routine ticks are cheap and only meaningful structural changes are surfaced; letting
Sali *interpret* a change is left to its next interaction, not forced here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from sali.obs.log import get_logger
from sali.twin.sync import SyncResult

log = get_logger("sali.twin.daemon")

OnChange = Callable[[SyncResult], Awaitable[None]]


class Refreshable(Protocol):
    """Anything the daemon can tick — TwinService, or a stub in tests."""

    async def refresh(self, *, exclude_projects: tuple[str, ...] = ...) -> SyncResult: ...


class TwinDaemon:
    """Continuously re-observe the machine and surface meaningful structural changes."""

    def __init__(
        self, service: Refreshable, *, interval: float = 180.0,
        exclude_projects: tuple[str, ...] = (),
    ) -> None:
        self.service = service
        self.interval = interval
        self.exclude = exclude_projects
        self.cycles = 0

    async def tick(self) -> SyncResult:
        """One observation cycle: refresh the twin and log if anything structurally changed."""
        result = await self.service.refresh(exclude_projects=self.exclude)
        self.cycles += 1
        if result.added or result.removed:
            log.info("twin_changed", cycle=self.cycles,
                     added=result.added, removed=result.removed)
        return result

    async def run(
        self, *, stop: asyncio.Event | None = None, max_cycles: int | None = None,
        on_change: OnChange | None = None, on_tick: Callable[[int], Awaitable[None]] | None = None,
    ) -> int:
        """Loop until ``stop`` is set (or ``max_cycles`` reached). A failing cycle is logged and
        skipped — a transient observation error never kills the watcher. Returns cycles run.
        ``on_tick`` fires every cycle (with the cycle number) — used to piggyback periodic work
        like learning consolidation on the same background service."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                result = await self.tick()
                if (result.added or result.removed) and on_change is not None:
                    await on_change(result)
                if on_tick is not None:
                    await on_tick(self.cycles)
            except Exception as exc:  # noqa: BLE001 - a bad cycle must not stop the watcher
                log.warning("twin_tick_failed", error=str(exc))
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            with contextlib.suppress(TimeoutError):  # sleep, but wake immediately on stop
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
        return self.cycles
