"""Task progress watchdog — detect potentially stuck tasks.

Distinguishes heartbeat (process alive) from progress (meaningful work done).

A task is NOT stuck merely because:
- it's running a long tool (npm install, docker build)
- the LLM is reasoning
- no step advanced during a tool execution

A task IS potentially stuck when:
- it's running (status='running')
- heartbeat is recent (process alive)
- no meaningful progress for an extended period
- no active tool execution

The watchdog does NOT auto-kill tasks. It only marks them as potentially_stuck
for observability. The user or recovery system decides what to do.

Thresholds are configurable via the Settings object.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.task_watchdog")


@dataclass
class WatchdogConfig:
    """Configurable thresholds for the progress watchdog."""
    # How long without meaningful progress before marking potentially_stuck
    progress_timeout: timedelta = timedelta(minutes=30)
    # How often to check active tasks
    check_interval: timedelta = timedelta(minutes=2)
    # How long without heartbeat before considering orphaned
    heartbeat_timeout: timedelta = timedelta(minutes=5)


# Sensible defaults
DEFAULT_CONFIG = WatchdogConfig()


class TaskWatchdog:
    """Periodically inspects active tasks and classifies their health.

    Health states:
    - healthy: recent progress or recently started
    - active_tool: a tool is currently executing (not stuck)
    - potentially_stuck: no progress for progress_timeout, no active tool
    - orphaned: no heartbeat for heartbeat_timeout

    The watchdog does NOT cancel, kill, or modify tasks.
    It only updates the health_status column for observability.
    """

    def __init__(self, pool: Any, config: WatchdogConfig | None = None, publisher: Any = None) -> None:
        self._pool = pool
        self._config = config or DEFAULT_CONFIG
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._publisher = publisher  # EventPublisher — set by runtime

    async def start(self) -> None:
        """Start the watchdog loop."""
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the watchdog loop."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        """Main watchdog loop."""
        while not self._stop.is_set():
            try:
                await self._check_all()
            except Exception as exc:  # noqa: BLE001
                log.warning("watchdog_check_failed", error=str(exc))
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._config.check_interval.total_seconds())

    async def _check_all(self) -> None:
        """Check all running primary tasks for health."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, objective, status, last_heartbeat, last_progress_at, "
                "  last_progress_type, active_tool_name, health_status, updated_at "
                "FROM task "
                "WHERE status = 'running' AND is_primary = true")

            for row in rows:
                health = self._classify(row)
                if health != row["health_status"]:
                    old_health = row["health_status"]
                    await conn.execute(
                        "UPDATE task SET health_status = $2, updated_at = now() "
                        "WHERE id = $1",
                        row["id"], health)
                    # Emit health transition event (only on change, not every interval)
                    await self._emit_health_transition(
                        conn, row["id"], old_health, health, row["objective"])
                    if health == "potentially_stuck":
                        log.info("task_potentially_stuck",
                                 task_id=str(row["id"])[:8],
                                 objective=row["objective"],
                                 last_progress=row["last_progress_at"])
                    elif health == "orphaned":
                        log.warning("task_orphaned",
                                    task_id=str(row["id"])[:8],
                                    objective=row["objective"])

    async def _emit_health_transition(
        self, conn: Any, task_id: Any, old_health: str, new_health: str, objective: str,
    ) -> None:
        """Emit a health transition event through the canonical publisher."""
        if self._publisher is not None:
            with contextlib.suppress(Exception):
                await self._publisher.emit(
                    event_type="task.health_changed", task_id=task_id,
                    subject_type="task", subject_id=task_id, origin="watchdog",
                    data={"old_health": old_health, "new_health": new_health,
                          "objective": objective[:200]})
        else:
            # Fallback: direct SQL
            with contextlib.suppress(Exception):
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                    "VALUES ($1, 'task', $2, $3)",
                    "task.health_changed", task_id,
                    {"old_health": old_health, "new_health": new_health,
                     "objective": objective[:200]})

    def _classify(self, row: Any) -> str:
        """Classify a task's health based on deterministic state."""
        from datetime import datetime
        now = datetime.now(UTC)

        # Terminal states — not our concern
        if row["status"] in ("done", "failed", "abandoned", "cancelled", "superseded"):
            return "healthy"

        # Active tool execution — not stuck
        if row["active_tool_name"]:
            return "active_tool"

        # Check heartbeat — if stale, task is orphaned
        heartbeat = row["last_heartbeat"]
        if heartbeat is not None:
            hb_age = now - heartbeat.replace(tzinfo=UTC) if heartbeat.tzinfo is None else now - heartbeat
            if hb_age > self._config.heartbeat_timeout:
                return "orphaned"

        # Check progress — if stale and no active tool, potentially stuck
        progress = row["last_progress_at"]
        if progress is not None:
            prog_age = now - progress.replace(tzinfo=UTC) if progress.tzinfo is None else now - progress
            if prog_age > self._config.progress_timeout:
                return "potentially_stuck"
        elif row["updated_at"] is not None:
            # No progress ever recorded — fall back to updated_at
            upd_age = now - row["updated_at"].replace(tzinfo=UTC) if row["updated_at"].tzinfo is None else now - row["updated_at"]
            if upd_age > self._config.progress_timeout:
                return "potentially_stuck"

        return "healthy"

    async def get_health_summary(self) -> list[dict[str, Any]]:
        """Get health summary of all active tasks (for API/diagnostics)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, objective, status, health_status, last_heartbeat, "
                "  last_progress_at, last_progress_type, active_tool_name "
                "FROM task "
                "WHERE status IN ('running', 'waiting', 'blocked', 'paused') "
                "ORDER BY updated_at DESC LIMIT 10")
        return [dict(r) for r in rows]
