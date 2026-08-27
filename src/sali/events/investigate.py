"""Attention → wake: drive one autonomous reasoning turn from an 'investigate' verdict (§12/§17).

The Attention Engine's whole point is that expensive reasoning wakes ONLY when something warrants it.
Its `investigate` verdict is documented as "the only action that may wake the model", but until now
nothing consumed it — so Sali only ever reasoned reactively, on Almir's turn. This closes that edge:
it watermark-polls for `investigate` observations and drives exactly ONE unattended turn per, framed
strictly as INVESTIGATE-AND-INFORM — Sali looks into what it noticed with read-only tools and states a
conclusion, it does NOT change or fix anything (§46/§59/§78); the unattended confirmer denies anything
destructive regardless. Bounded (one per tick, watermarked so each fires once) so it can't storm.

The runner (the agent loop) is INJECTED — the events layer never imports runtime — so this stays a
thin, testable consumer.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sali.events.bus import wait_or_wake
from sali.obs.log import get_logger

log = get_logger("sali.events.investigate")


class InvestigateLoop:
    def __init__(self, pool: Any, runner: Any, *, interval: float = 30.0, max_per_tick: int = 1) -> None:
        self._pool = pool
        self._runner = runner  # duck-typed: async run(prompt) -> anything
        self._interval = interval
        self._max = max_per_tick
        self._watermark = -1

    async def _ensure_watermark(self, conn: Any) -> None:
        if self._watermark < 0:  # start from the tip so a restart doesn't re-investigate a backlog
            self._watermark = int(await conn.fetchval("SELECT COALESCE(max(seq), 0) FROM event") or 0)

    @staticmethod
    def _prompt(payload: dict[str, Any]) -> str:
        summary = str(payload.get("summary") or "something changed on the machine")
        return (f"While watching the machine you noticed: {summary}. Look into what it is and whether it "
                "matters — you may use read-only tools to check. Do NOT change, fix, disable, or restart "
                "anything; just investigate and state your conclusion plainly.")

    async def tick(self, *, drive: bool = True) -> list[str]:
        """Drive an autonomous turn for each new investigate observation. Returns the prompts used."""
        async with self._pool.acquire() as conn:
            await self._ensure_watermark(conn)
            rows = await conn.fetch(
                "SELECT seq, payload FROM event WHERE event_type='desktop.observed' AND seq > $1 "
                "AND payload->>'action' = 'investigate' ORDER BY seq LIMIT $2",
                self._watermark, self._max)
        driven: list[str] = []
        for r in rows:
            prompt = self._prompt(r["payload"])
            if drive:
                with contextlib.suppress(Exception):  # a bad turn must never kill the faculty
                    await self._runner.run(prompt)
            async with self._pool.acquire() as conn:  # audit: what Sali investigated on its own
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('sali.investigated','desktop',$1)", {"source_seq": r["seq"]})
            self._watermark = max(self._watermark, int(r["seq"]))
            driven.append(prompt)
        if driven:
            log.info("investigated", count=len(driven))
        return driven

    async def run(self, stop: asyncio.Event, wake: asyncio.Event | None = None) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - never let a bad cycle kill the faculty
                log.warning("investigate tick failed: %s", exc)
            await wait_or_wake(stop, wake, self._interval)  # push when subscribed, else heartbeat
