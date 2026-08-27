"""The Proactive loop (spec §16/§74–78) — Sali speaks up on its own, without being asked.

The Attention Engine tags each observation with an action; the ones it marks `notify` (critical:
a service failed, a disk filling) or `investigate` (a new listening service) are exactly the things
worth telling Almir promptly. This loop watches for those and delivers a short desktop notification —
deterministically composed from the observation, NO model in the loop (§14/§79) — then records that it
announced them so it never repeats itself.

Crucially it only INFORMS; it never acts on the machine (§46/§78): "a new service is listening on
port X — I haven't touched it." Attention already gates the volume to the genuinely-notable, so this
respects "don't annoy Almir" (§15) by construction. On startup it sets its watermark to the current
tip, so a restart never dumps a backlog of stale notifications.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sali.events.bus import wait_or_wake
from sali.obs.log import get_logger
from sali.tools.builtins.notify_tool import _desktop_env, _notify_send

log = get_logger("sali.events.proactive")

_ACTIONS = ("notify", "investigate")  # attention verdicts worth surfacing proactively


def compose(payload: dict[str, Any]) -> tuple[str, str]:
    """A short (title, body) for a surfaced observation — deterministic, in Sali's voice, and always
    making clear Sali has NOT acted (§78)."""
    kind = str(payload.get("kind", ""))
    summary = str(payload.get("summary", "something changed"))
    sample = str((payload.get("detail") or {}).get("sample", ""))
    if kind == "service_failed":
        return "Sali — service failed", f"{sample or summary} failed. It was running before; I haven't touched it."
    if kind == "disk_pressure":
        return "Sali — disk almost full", f"{sample or 'A disk'} is nearly full."
    if kind == "port_opened":
        return "Sali — new service", f"A new service is listening on {sample or 'a port'}. It wasn't there before — I haven't touched it."
    return "Sali", summary


class ProactiveLoop:
    def __init__(self, pool: Any, *, interval: float = 20.0, max_per_tick: int = 3) -> None:
        self._pool = pool
        self._interval = interval
        self._max = max_per_tick
        self._watermark = -1  # event seq; -1 until initialised to the current tip

    async def _ensure_watermark(self, conn: Any) -> None:
        if self._watermark < 0:  # first run: start from the tip so no stale backlog is announced
            self._watermark = int(await conn.fetchval("SELECT COALESCE(max(seq), 0) FROM event") or 0)

    async def tick(self, *, deliver: bool = True) -> list[str]:
        """Announce any new notify/investigate observations. Returns the bodies delivered."""
        async with self._pool.acquire() as conn:
            await self._ensure_watermark(conn)
            rows = await conn.fetch(
                "SELECT seq, payload FROM event WHERE event_type='desktop.observed' AND seq > $1 "
                "AND payload->>'action' = ANY($2) ORDER BY seq LIMIT $3",
                self._watermark, list(_ACTIONS), self._max)
        delivered: list[str] = []
        env = _desktop_env()
        for r in rows:
            title, body = compose(r["payload"])
            if deliver:
                with contextlib.suppress(Exception):  # delivery is best-effort, never crashes the loop
                    _notify_send(title, body, env)
            async with self._pool.acquire() as conn:  # audit trail — what Sali told Almir, and when
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('sali.proactive','desktop',$1)", {"source_seq": r["seq"], "message": body})
            self._watermark = max(self._watermark, int(r["seq"]))
            delivered.append(body)
        if delivered:
            log.info("proactive", count=len(delivered))
        return delivered

    async def run(self, stop: asyncio.Event, wake: asyncio.Event | None = None) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - never let a bad cycle kill the faculty
                log.warning("proactive tick failed: %s", exc)
            await wait_or_wake(stop, wake, self._interval)  # push when subscribed, else heartbeat
