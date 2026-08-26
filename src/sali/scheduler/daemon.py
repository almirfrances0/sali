"""SchedulerDaemon — fire due schedules as autonomous Sali turns (§44).

Same shape as TwinDaemon: a poll loop that never dies on a bad tick. It stays decoupled from the
runtime by firing through an injected Runner (the CLI wires the AgentLoop), so this faculty sits
cleanly below runtime. Firing is opt-in autonomy — Almir created the schedule — and every fire is
recorded (last_status) and emitted to the append-only event log for audit (§24/§25).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

from sali.obs.log import get_logger
from sali.scheduler.store import ScheduleStore

log = get_logger("sali.scheduler")


class Runner(Protocol):
    """Runs a scheduled prompt as a full Sali turn. The CLI injects an AgentLoop adapter."""

    async def run(self, prompt: str) -> Any: ...


class SchedulerDaemon:
    def __init__(
        self, store: ScheduleStore, runner: Runner, *, pool: Any = None, poll_s: float = 30.0
    ) -> None:
        self.store = store
        self.runner = runner
        self.pool = pool
        self.poll_s = poll_s
        self._stop = asyncio.Event()

    async def tick(self) -> int:
        """Fire every currently-due schedule once, record the outcome, advance its next time.
        Returns how many fired. A failing fire is logged and recorded, never fatal."""
        due = await self.store.due()
        for sched in due:
            try:
                await self.runner.run(sched.prompt)
                status = "ok"
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the scheduler
                status = f"error: {exc}"[:200]
                log.warning("schedule_fire_failed", name=sched.name, error=str(exc))
            await self.store.mark_fired(sched.id, status)
            await self._emit_fired(sched.name, status)
        return len(due)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                fired = await self.tick()
                if fired:
                    log.info("schedules_fired", count=fired)
            except Exception as exc:  # noqa: BLE001 - a bad tick never kills the daemon
                log.warning("scheduler_tick_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)

    def stop(self) -> None:
        self._stop.set()

    async def _emit_fired(self, name: str, status: str) -> None:
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('schedule.fired', 'schedule', $1)",
                    {"name": name, "status": status})
        except Exception as exc:  # noqa: BLE001 - audit is best-effort, never break the daemon
            log.warning("schedule_event_failed", error=str(exc))
