"""Burst aggregation — collapse a flurry of related events into one observation (sali3 §11,42).

A save-all, a `git checkout`, a build touching thousands of files: without aggregation each becomes an
event and floods the context. Events are bucketed by (kind, directory / app); a bucket is flushed —
as a single Observation carrying a `count` — once it's been quiet for `window_s` or hits `max_count`.
No clock is held: the engine passes `now`, so aggregation is fully deterministic under test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from sali.events.base import DesktopEvent, EventKind, Observation

_MAX_TARGETS = 512  # distinct paths tracked per bucket, so a huge burst can't grow the set unbounded


@dataclass
class _Bucket:
    kind: EventKind
    where: str  # directory (file events) or app (window)
    count: int  # raw event count (a single file saved 20× counts 20)
    first_at: datetime
    last_at: datetime
    importance: float
    sample: str  # a representative target, to name a single-file bucket well
    targets: set[str]  # DISTINCT targets (capped) — so we can say "20 changes to 1 file" vs "20 files"
    capped: bool = False  # True once distinct targets hit the cap (so counts read as "N+")
    # Whatever the source event carried beyond its target — for a window focus, the TITLE. Without
    # this the row said "focused code" and nothing else: the app but never the document, which is the
    # half that says what Almir is actually working ON.
    extra: dict = None  # type: ignore[assignment]


class Aggregator:
    def __init__(self, *, window_s: float = 2.0, max_count: int = 5000, max_age_s: float = 30.0) -> None:
        self.window_s = window_s
        self.max_count = max_count
        self.max_age_s = max_age_s  # a steadily-active bucket still surfaces periodically (no starving)
        self._buckets: dict[str, _Bucket] = {}

    def add(self, event: DesktopEvent, importance: float) -> None:
        key = self._key(event)
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(
                kind=event.kind, where=self._where(event), count=1, first_at=event.at,
                last_at=event.at, importance=importance, sample=event.target, targets={event.target},
                extra=dict(getattr(event, "extra", None) or {}))
            return
        bucket.count += 1
        bucket.last_at = event.at
        bucket.importance = max(bucket.importance, importance)
        bucket.sample = event.target
        # Newest wins: within one window a person switches documents inside the same app, and the
        # title he is on NOW is the one worth reporting.
        if getattr(event, "extra", None):
            bucket.extra = dict(event.extra)
        if len(bucket.targets) < _MAX_TARGETS:
            bucket.targets.add(event.target)
        else:
            bucket.capped = True

    def flush_due(self, now: datetime) -> list[Observation]:
        """Emit observations for buckets that have gone quiet, overflowed, or aged out. Mutates state."""
        out: list[Observation] = []
        for key, bucket in list(self._buckets.items()):
            quiet = (now - bucket.last_at).total_seconds() >= self.window_s
            aged = (now - bucket.first_at).total_seconds() >= self.max_age_s
            if quiet or aged or bucket.count >= self.max_count:
                out.append(self._observe(bucket))
                del self._buckets[key]
        return out

    def flush_all(self) -> list[Observation]:
        """Emit everything still buffered (on shutdown). Mutates state."""
        out = [self._observe(b) for b in self._buckets.values()]
        self._buckets.clear()
        return out

    # ── internals ────────────────────────────────────────────────────────────

    def _key(self, event: DesktopEvent) -> str:
        return f"{event.kind.value}:{self._where(event)}"

    def _where(self, event: DesktopEvent) -> str:
        if event.kind is EventKind.WINDOW_FOCUS:
            return event.target
        parent = event.target.rsplit(os.sep, 1)[0] if os.sep in event.target else "."
        return parent or os.sep

    def _observe(self, b: _Bucket) -> Observation:
        detail = {"where": b.where, "sample": b.sample, "files": len(b.targets)}
        if b.extra:
            detail.update({k: v for k, v in b.extra.items() if v})
        return Observation(
            kind=b.kind, summary=self._summary(b), importance=b.importance, count=b.count,
            first_at=b.first_at, last_at=b.last_at, detail=detail)

    def _summary(self, b: _Bucket) -> str:
        if b.kind is EventKind.WINDOW_FOCUS:
            # "focused code — voice.py" beats "focused code". The app alone tells you he is in an
            # editor; the title tells you what he is editing, which is the thing worth knowing.
            title = str((b.extra or {}).get("title") or "").strip()
            return f"focused {b.where}" + (f" — {title}" if title else "")
        verb = {
            EventKind.FILE_CREATED: "created", EventKind.FILE_MODIFIED: "modified",
            EventKind.FILE_DELETED: "deleted", EventKind.FILE_MOVED: "moved",
        }.get(b.kind, "changed")
        name = b.sample.rsplit(os.sep, 1)[-1]
        distinct = len(b.targets)
        more = "+" if b.capped else ""
        if distinct <= 1:  # one file touched — say how many times, not "N files"
            return f"{verb} {name}" if b.count == 1 else f"{verb} {name} ({b.count}×)"
        where = b.where.rsplit(os.sep, 1)[-1] or b.where
        return f"{verb} {distinct}{more} files in {where}"
