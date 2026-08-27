"""Phase 1 · Increment 5 — the Proactive loop (§16/§74–78/§46).

Sali surfaces the attention-flagged notify/investigate observations to Almir on its own, deterministic
messages that make clear it hasn't acted, announced exactly once, and never a backlog dump on restart.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.events.proactive import ProactiveLoop, compose

pytestmark = pytest.mark.db


def test_compose_messages_make_clear_sali_did_not_act() -> None:
    _, body = compose({"kind": "port_opened", "summary": "new listening socket tcp:0.0.0.0:8080",
                        "detail": {"sample": "tcp:0.0.0.0:8080"}})
    assert "8080" in body and "haven't touched it" in body
    _, failed = compose({"kind": "service_failed", "detail": {"sample": "nginx.service"}})
    assert "nginx.service" in failed and "haven't touched it" in failed
    _, disk = compose({"kind": "disk_pressure", "detail": {"sample": "/"}})
    assert "nearly full" in disk


async def _observed(conn: Any, kind: str, action: str, sample: str) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES ('desktop.observed','desktop',$1)",
        {"kind": kind, "summary": f"{kind} {sample}", "action": action, "detail": {"sample": sample}})


async def test_announces_notify_observations_once(live_pool: Any) -> None:
    loop = ProactiveLoop(live_pool)
    await loop.tick(deliver=False)  # loop starts: watermark set to the tip (baseline)

    async with live_pool.acquire() as conn:  # new observations arrive AFTER startup
        await _observed(conn, "port_opened", "record", "tcp:0.0.0.0:1")  # 'record' → not surfaced
        await _observed(conn, "service_failed", "notify", "nginx.service")
        await _observed(conn, "port_opened", "investigate", "tcp:0.0.0.0:8080")

    first = await loop.tick(deliver=False)
    assert len(first) == 2 and any("nginx.service" in m for m in first)
    # the 'record'-tagged observation was NOT surfaced (attention said don't interrupt)
    assert all("tcp:0.0.0.0:1" not in m for m in first)

    second = await loop.tick(deliver=False)
    assert second == []  # announced once, never repeated

    # each announcement left an audit trail
    audited = await live_pool.acquire()
    try:
        n = await audited.fetchval("SELECT count(*) FROM event WHERE event_type='sali.proactive'")
    finally:
        await live_pool.release(audited)
    assert n == 2


async def test_restart_does_not_dump_backlog(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:  # an old notify observation from before this "boot"
        await _observed(conn, "service_failed", "notify", "old.service")

    fresh = ProactiveLoop(live_pool)  # a freshly-started loop sets its watermark to the current tip
    assert await fresh.tick(deliver=False) == []  # the backlog is not announced on startup

    async with live_pool.acquire() as conn:  # but a NEW one after startup is
        await _observed(conn, "service_failed", "notify", "new.service")
    delivered = await fresh.tick(deliver=False)
    assert len(delivered) == 1 and "new.service" in delivered[0]
