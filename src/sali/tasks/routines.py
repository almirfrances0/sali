"""RoutineStore (§8) — intentional recurring activities Sali understands.

A routine is more than a cron job: it has a purpose, conditions, and an outcome/failure history Sali
reasons about (check project health, review learning candidates, monitor a service). Timing reuses the
existing scheduler semantics (interval / cron) via ``scheduler.cron`` — this is not a second scheduler,
just a durable record of the recurring intent + its next fire time + how it has been going.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sali.core.clock import Clock, SystemClock

# Routines are interval-driven here (the tasks layer can't import the sibling scheduler layer). Cron-style
# schedules remain the existing ScheduleStore's job; a routine is Sali's higher-level recurring INTENT.
_INTERVAL = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _interval_delta(spec: str) -> timedelta:
    m = _INTERVAL.match(spec)
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"a routine schedule must be a positive interval like '30m'/'2h'/'1d': {spec!r}")
    return timedelta(**{_UNIT[m.group(2).lower()]: int(m.group(1))})


class RoutineStore:
    def __init__(self, pool: Any, publisher: Any = None, clock: Clock | None = None) -> None:
        self._pool = pool
        self._publisher = publisher
        self._clock = clock or SystemClock()

    async def create(
        self, *, name: str, when: str, purpose: str | None = None,
        conditions: dict[str, Any] | None = None,
    ) -> UUID:
        """Register a recurring activity. `when` is an interval ('30m'/'2h'/'1d'); raises ValueError on a
        spec that isn't a valid interval so a typo can't create a routine that never (or wrongly) fires."""
        spec = when.strip().lower().replace(" ", "")
        next_at = self._clock.now() + _interval_delta(spec)
        rid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO routine (id, name, purpose, schedule_kind, schedule_spec, conditions, "
                "  next_execution) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (name) DO UPDATE SET purpose=excluded.purpose, "
                "  schedule_kind=excluded.schedule_kind, schedule_spec=excluded.schedule_spec, "
                "  next_execution=excluded.next_execution, updated_at=now()",
                rid, name, purpose, "interval", spec, conditions or {}, next_at)
            row = await conn.fetchrow("SELECT id FROM routine WHERE name=$1", name)
        return UUID(str(row["id"]))

    async def due(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Enabled routines whose fire time has arrived — a wake signal for the life loop (§37)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, purpose, conditions, next_execution FROM routine "
                "WHERE enabled AND next_execution <= $1 ORDER BY next_execution LIMIT $2",
                self._clock.now(), limit)
        return [dict(r) for r in rows]

    async def mark_executed(self, routine_id: UUID, *, success: bool, result: str | None = None) -> None:
        """Record an execution and advance to the next fire time (measured from now, so a slow/missed
        run never causes a burst of catch-up fires). Tracks a failure streak Sali can reason about."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, schedule_spec FROM routine WHERE id=$1", routine_id)
            if row is None:
                return
            next_at = self._clock.now() + _interval_delta(row["schedule_spec"])
            await conn.execute(
                "UPDATE routine SET last_execution=$2, last_result=$3, next_execution=$4, "
                "  failure_count = CASE WHEN $5 THEN 0 ELSE failure_count + 1 END, updated_at=now() "
                "WHERE id=$1", routine_id, self._clock.now(), (result or "")[:500], next_at, success)
        await self._emit("routine.completed" if success else "routine.failed",
                         {"routine_id": str(routine_id), "name": row["name"]})

    async def next_wake(self) -> datetime | None:
        """The earliest upcoming routine fire time — feeds the life loop's next-wake computation (§37)."""
        async with self._pool.acquire() as conn:
            val: datetime | None = await conn.fetchval(
                "SELECT min(next_execution) FROM routine WHERE enabled")
        return val

    async def set_enabled(self, name: str, enabled: bool) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE routine SET enabled=$2, updated_at=now() WHERE name=$1",
                               name, enabled)

    async def list(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, purpose, enabled, next_execution, last_execution, last_result, "
                "  failure_count FROM routine ORDER BY name")
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            total = int(await conn.fetchval("SELECT count(*) FROM routine WHERE enabled") or 0)
            due = int(await conn.fetchval(
                "SELECT count(*) FROM routine WHERE enabled AND next_execution <= now()") or 0)
        return {"enabled": total, "due": due}

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="routine", origin="background",
                                       data=data)
