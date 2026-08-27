"""Architecture review · Increment 7 — attention 'investigate' wakes one autonomous turn (§12/§17).

Closes the autonomous-inference edge: an investigate verdict drives exactly one investigate-and-inform
turn through the agent loop; a notify verdict does not; each fires once; the prompt forbids acting.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.events.investigate import InvestigateLoop

pytestmark = pytest.mark.db


class _RecordingRunner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> None:
        self.prompts.append(prompt)


async def _observed(conn: Any, action: str, summary: str) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES ('desktop.observed','desktop',$1)",
        {"kind": "port_opened", "summary": summary, "action": action})


async def test_only_investigate_verdicts_wake_a_turn(live_pool: Any) -> None:
    runner = _RecordingRunner()
    loop = InvestigateLoop(live_pool, runner)
    await loop.tick()  # baseline: watermark to the tip

    async with live_pool.acquire() as conn:
        await _observed(conn, "notify", "a disk is full")                # must NOT wake a turn
        await _observed(conn, "investigate", "a new service on tcp:9000")  # must wake one

    driven = await loop.tick()
    assert len(driven) == 1 and runner.prompts == driven
    assert "tcp:9000" in driven[0]
    # the autonomous turn is investigate-and-inform, never act (§78)
    assert "Do NOT change" in driven[0] and "investigate" in driven[0].lower()


async def test_each_investigate_fires_exactly_once(live_pool: Any) -> None:
    runner = _RecordingRunner()
    loop = InvestigateLoop(live_pool, runner)
    await loop.tick()
    async with live_pool.acquire() as conn:
        await _observed(conn, "investigate", "unknown process appeared")

    assert len(await loop.tick()) == 1
    assert await loop.tick() == []  # watermark advanced — never re-driven
    assert len(runner.prompts) == 1

    # and an audit event was written for what Sali investigated
    async with live_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM event WHERE event_type='sali.investigated'") == 1


async def test_restart_does_not_investigate_a_backlog(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:  # a stale investigate from before this "boot"
        await _observed(conn, "investigate", "old thing")
    fresh = InvestigateLoop(live_pool, _RecordingRunner())
    assert await fresh.tick() == []  # watermark starts at the tip; no backlog storm
