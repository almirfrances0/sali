"""Clock abstraction.

All Sali timestamps are timezone-aware UTC. Injecting a clock lets tests freeze time
deterministically instead of sleeping or reading the wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Real wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock for tests; advance() moves it forward explicitly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._t = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)
