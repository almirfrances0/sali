"""ScheduleStore — persist schedules and compute their fire times (§44).

The store owns a clock so it can derive next_run_at deterministically (inject a fake clock in
tests). Firing is done by the daemon; the store just records the outcome and advances the schedule
to its next time, so a schedule never double-fires or drifts.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.clock import Clock, SystemClock
from sali.scheduler import cron
from sali.scheduler.models import Schedule, row_to_schedule


class ScheduleStore:
    def __init__(self, pool: Any, clock: Clock | None = None,
                 owner_timezone: str | None = None) -> None:
        self.pool = pool
        self.clock = clock or SystemClock()
        # The zone a new cron schedule is written in. Stored per row rather than read at firing time,
        # so a schedule Almir set while in one place keeps meaning what he meant when he set it.
        self.owner_timezone = owner_timezone or "UTC"

    async def create(self, name: str, when: str, prompt: str,
                     timezone: str | None = None) -> Schedule:
        """Register a schedule. `when` is an interval ('30m') or 5-field cron ('0 9 * * *');
        cron.parse_when raises ScheduleError on a spec that would never (or wrongly) fire.

        A cron spec is civil time in `timezone` (the owner's zone by default) — "0 9 * * *" is nine in
        the morning where he is, not 09:00 UTC. Intervals are durations and ignore it."""
        kind, spec = cron.parse_when(when)
        zone = timezone or self.owner_timezone
        next_at = cron.next_run(kind, spec, self.clock.now(), zone)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO schedule (name, kind, spec, prompt, next_run_at, timezone) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                name, kind, spec, prompt, next_at, zone)
        return row_to_schedule(row)

    async def due(self) -> list[Schedule]:
        """Enabled schedules whose fire time has arrived, oldest-due first."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM schedule WHERE enabled AND next_run_at <= $1 ORDER BY next_run_at",
                self.clock.now())
        return [row_to_schedule(r) for r in rows]

    async def mark_fired(self, schedule_id: UUID, status: str) -> None:
        """Record the outcome and advance to the next fire time (measured from now, so a slow or
        missed run never causes a burst of catch-up fires)."""
        now = self.clock.now()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kind, spec, timezone FROM schedule WHERE id = $1", schedule_id)
            if row is None:
                return
            # Re-derived in the schedule's own zone at EVERY firing. That is the whole point: a fixed
            # UTC instant computed once would drift by an hour the moment the zone changes offset.
            next_at = cron.next_run(row["kind"], row["spec"], now, row["timezone"])
            await conn.execute(
                "UPDATE schedule SET last_run_at = $1, last_status = $2, next_run_at = $3, "
                "updated_at = now() WHERE id = $4",
                now, status[:500], next_at, schedule_id)

    async def list_all(self) -> list[Schedule]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM schedule ORDER BY name")
        return [row_to_schedule(r) for r in rows]

    async def set_enabled(self, name: str, enabled: bool) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE schedule SET enabled = $1, updated_at = now() WHERE name = $2", enabled, name)
        return _affected(result)

    async def delete(self, name: str) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM schedule WHERE name = $1", name)
        return _affected(result)


def _affected(tag: str) -> int:
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0
