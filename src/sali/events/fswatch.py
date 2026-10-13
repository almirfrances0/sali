"""Filesystem event source via watchdog (inotify on Linux). Import-guarded: no watchdog → unavailable.

watchdog runs its own OS thread; its callbacks hop back onto the asyncio loop with
``call_soon_threadsafe`` so the engine's queue is only ever touched from the loop thread. This module
is exercised live (CI has no filesystem-event harness), so it degrades cleanly and never raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sali.events.base import DesktopEvent, EventKind
from sali.obs.log import get_logger

log = get_logger("sali.events.fswatch")

Emit = Callable[[DesktopEvent], None]


class FsWatchSource:
    def __init__(self, roots: list[str]) -> None:
        # Only watch roots that exist and are directories — a missing root is skipped, not fatal.
        self._roots = [str(Path(r).expanduser()) for r in roots if Path(r).expanduser().is_dir()]
        self._observer: Any = None

    @property
    def available(self) -> bool:
        return bool(self._roots)

    def start(self, loop: asyncio.AbstractEventLoop, emit: Emit) -> bool:
        """Begin watching. Returns False (unavailable) if watchdog is absent or no root is watchable."""
        if not self._roots:
            return False
        try:
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog not installed — filesystem perception is off (pip install watchdog)")
            return False
        # Everything after the import is wrapped: a broken inotify backend must degrade (the engine
        # runs without fs events), never crash the daemon (§46 never crash the loop).
        try:
            handler = _Handler(loop, emit)
            observer = Observer()
            for root in self._roots:
                try:
                    observer.schedule(cast(Any, handler), root, recursive=True)
                except OSError as exc:  # e.g. inotify watch limit — skip that root, keep the rest
                    log.warning("could not watch %s: %s", root, exc)
            observer.daemon = True
            observer.start()
        except Exception as exc:  # noqa: BLE001 - degrade to no-fs-source, never propagate
            log.warning("filesystem perception could not start: %s", exc)
            return False
        self._observer = observer
        return True

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass
            self._observer = None


class _Handler:
    """Delegates watchdog callbacks onto the loop thread. Not a FileSystemEventHandler subclass at
    import time (watchdog may be absent); watchdog only calls the dispatch method it needs."""

    def __init__(self, loop: asyncio.AbstractEventLoop, emit: Emit) -> None:
        self._loop = loop
        self._emit = emit

    def dispatch(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return  # directory-level churn is noise; we track file events
        kind = {
            "created": EventKind.FILE_CREATED, "modified": EventKind.FILE_MODIFIED,
            "deleted": EventKind.FILE_DELETED, "moved": EventKind.FILE_MOVED,
        }.get(getattr(event, "event_type", ""))
        if kind is None:
            return
        target = getattr(event, "dest_path", "") or getattr(event, "src_path", "")
        if not target:
            return
        desktop_event = DesktopEvent(kind=kind, target=str(target), at=datetime.now(UTC), source="fs")
        self._loop.call_soon_threadsafe(self._emit, desktop_event)
