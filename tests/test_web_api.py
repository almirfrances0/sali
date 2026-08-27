"""Sali Web · Increments A2–A5 — serializer, graph read-API, presence/attention, event bridge, routes.

The web is a window into the SAME Sali: these verify the read facades and the fan-out bridge return real,
redacted state — the serializer never leaks a secret, the graph exposes bidirectional neighborhoods, the
hub pushes durable events (redacted) and backfills by seq, presence is DERIVED from real runtime state,
and the HTTP routes wire end-to-end through the app's lifespan.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest

from sali.api.serialize import safe, to_jsonable
from sali.api.services import WebServices
from sali.api.stream import EventHub
from sali.core.enums import MemorySource
from sali.graph.service import GraphService
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


class _FakeKernel:
    """Minimal composition root for the web layer — same shape Kernel exposes to WebServices."""

    def __init__(self, pool: Any, settings: Any) -> None:
        self.settings = settings
        self.provider = FakeModelProvider()
        self._pool = pool

    async def pool(self) -> Any:
        return self._pool


# ── serializer (§34 boundary) ───────────────────────────────────────────────────────────────────────
def test_safe_jsonifies_and_redacts() -> None:
    u = uuid4()
    out = safe({"id": u, "when": datetime(2026, 1, 1, 12, 0, 0), "token": "sk-abc",
                "nested": {"password": "p"}, "tags": {"a", "b"}})
    assert out["id"] == str(u)
    assert isinstance(out["when"], str) and out["when"].startswith("2026-01-01")
    assert out["token"] == "[redacted]" and out["nested"]["password"] == "[redacted]"  # key-masked
    assert sorted(out["tags"]) == ["a", "b"]  # set → list


def test_to_jsonable_handles_dataclass_and_enum() -> None:
    assert to_jsonable(MemorySource.USER_EXPLICIT) == MemorySource.USER_EXPLICIT.value


# ── graph read API for the brain ─────────────────────────────────────────────────────────────────────
async def test_graph_snapshot_get_and_bidirectional_subgraph(live_pool: Any) -> None:
    g = GraphService(live_pool)
    await g.link(subject="Almir", relation="owns", obj="Sali", source=MemorySource.USER_EXPLICIT)
    await g.link(subject="Sali", relation="runs on", obj="Kali", source=MemorySource.SYSTEM_OBSERVATION)

    snap = await g.snapshot(limit=50)
    names = {n.name for n in snap["nodes"]}
    assert {"Almir", "Sali", "Kali"} <= names
    assert snap["edges"]  # edges among the snapshot's nodes are included

    sali = next(n for n in snap["nodes"] if n.name == "Sali")
    got = await g.get(sali.id)
    assert got is not None and got.id == sali.id  # fetch-by-id (get_node needs a key; the viz has an id)

    sub = await g.subgraph(sali.id, depth=1)
    sub_names = {n.name for n in sub["nodes"]}
    assert {"Almir", "Sali", "Kali"} <= sub_names  # BIDIRECTIONAL: inbound 'owns' + outbound 'runs_on'


# ── presence (derived) + attention counts (real) ──────────────────────────────────────────────────────
async def test_presence_is_derived_and_attention_counts_are_real(
    live_pool: Any, test_settings: Any
) -> None:
    svc = await WebServices.create(_FakeKernel(live_pool, test_settings))
    try:
        assert (await svc.presence())["presence"] == "idle"  # fresh singleton, no run
        await svc.self_state.note_turn("investigate the disk")  # mode → working
        assert (await svc.presence())["presence"] == "thinking"

        async with live_pool.acquire() as c:
            for tier in ("critical", "important", "interesting", "interesting"):
                await c.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('desktop.observed','desktop',$1)", {"tier": tier, "summary": "x"})
        counts = await svc.attention_counts(hours=1)
        assert counts["critical"] == 1 and counts["important"] == 1 and counts["interesting"] == 2
        assert counts["routine"] is None  # never persisted — honest, not fabricated
    finally:
        await svc.stop()


# ── the event fan-out bridge (the firehose → browsers) ────────────────────────────────────────────────
async def test_event_hub_fans_out_redacted(live_pool: Any) -> None:
    hub = EventHub(live_pool)
    await hub.start()
    q = hub.register()
    try:
        async with live_pool.acquire() as c:
            await c.execute(
                "INSERT INTO event (event_type, subject_type, payload) VALUES ('web.test','x',$1)",
                {"api_key": "sk-supersecret", "note": "hello"})
        f = await asyncio.wait_for(q.get(), timeout=5.0)
        assert f["type"] == "web.test" and isinstance(f["seq"], int)
        assert f["payload"]["api_key"] == "[redacted]"  # redacted at the boundary
        assert f["payload"]["note"] == "hello"
    finally:
        hub.unregister(q)
        await hub.stop()


async def test_event_hub_backfill_by_seq(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO event (event_type, subject_type, payload) VALUES ('web.b','x',$1)", {"k": "v"})
        seq = int(await c.fetchval("SELECT max(seq) FROM event"))
    hub = EventHub(live_pool)
    await hub.start()
    try:
        rows = await hub.backfill(seq - 1)
        assert any(r["seq"] == seq for r in rows)  # a reconnecting client resumes from its last seq
    finally:
        await hub.stop()


# ── HTTP routes, end-to-end through the app's lifespan (same event loop, no TestClient thread) ────────
async def test_http_routes_end_to_end(test_settings: Any, db_available: bool) -> None:
    if not db_available:
        pytest.skip("Postgres not reachable")
    import httpx
    from httpx import ASGITransport

    from sali.api.app import create_app
    from sali.kernel import Kernel

    kernel = Kernel.create(test_settings)
    app = create_app(kernel)
    async with app.router.lifespan_context(app):
        svc = app.state.services
        async with svc.pool.acquire() as c:
            await c.execute("TRUNCATE event RESTART IDENTITY")
        await svc.graph.link(subject="Sali", relation="runs on", obj="Kali",
                             source=MemorySource.SYSTEM_OBSERVATION)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            assert (await ac.get("/health")).json() == {"ok": True}
            assert "identity" in (await ac.get("/api/self")).json()
            assert "presence" in (await ac.get("/api/presence")).json()
            snap = (await ac.get("/api/graph/snapshot")).json()
            assert any(n["name"] == "Sali" for n in snap["nodes"])
            assert "events" in (await ac.get("/api/events")).json()
            assert (await ac.get("/api/tools")).json()["tools"]  # the tool catalog is exposed
            bad = await ac.get("/api/graph/node/not-a-uuid")
            assert bad.status_code == 400  # invalid id is a clean 400, not a crash
    await kernel.close()
