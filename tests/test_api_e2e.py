"""End-to-end iPhone control-center scenario (Prompt 13 §43).

One cohesive arc, over the real API + durable event log:

  fresh app → one-time enrollment → device session → authenticated request → authenticated WebSocket →
  user sends chat → Sali works (task + live activity) → agent-initiated message → app backgrounds
  (disconnect) → work continues while offline → reconnect → missed events recovered → artifact delivered →
  task inspected → system health elevated → preservation event streamed → natural instruction → state updates

Nothing here loads a model (RuntimeStub records messages); the point is the transport, security, recovery,
and state contract the iOS app depends on — proven against PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sali.api.devices import DeviceStore
from sali.api.ws import manager
from sali.events.publisher import EventPublisher
from sali.tasks.revocation import RevocationStore
from sali.tasks.store import TaskStore
from tests.apiutil import FakeWebSocket, RuntimeStub, auth, build_app, client, run_ws

pytestmark = pytest.mark.db


def _reset_manager() -> None:
    manager._connections.clear()
    manager._last_activity.clear()
    manager._last_event_seq.clear()


async def test_full_iphone_control_center_scenario(live_pool: Any, tmp_path: Path) -> None:
    _reset_manager()
    pub = EventPublisher(live_pool)
    runtime = RuntimeStub(live_pool)
    app = build_app(live_pool, runtime=runtime)

    async with client(app) as c:
        # 1) FRESH APP → ONE-TIME ENROLLMENT. Host mints a pairing code; the fresh device redeems it (§23).
        code, _ = await DeviceStore(live_pool).mint_enrollment_code(role="owner", label="Almir's iPhone")
        enrolled = (await c.post("/api/v1/enroll",
                                 json={"code": code, "name": "Almir's iPhone", "model": "iPhone16,1"})).json()
        access = enrolled["access_token"]
        assert enrolled["role"] == "owner" and access

        # 2) AUTHENTICATED REQUEST works; an unauthenticated one is rejected (§43).
        assert (await c.get("/api/v1/status", headers=auth(access))).status_code == 200
        assert (await c.get("/api/v1/status")).status_code == 401

        # 3) AUTHENTICATED WEBSOCKET connects and subscribes from the start (§25/§29).
        ws1 = FakeWebSocket(incoming=[{"type": "subscribe", "after_seq": 0}])
        await run_ws(ws1, access, live_pool)
        assert ws1.accepted is True and ws1.closed_code is None

        # 4) USER SENDS A CHAT MESSAGE (§5).
        r = await c.post("/api/v1/conversation/message",
                         json={"content": "Build me a landing page"}, headers=auth(access))
        assert r.status_code == 200 and "Build me a landing page" in runtime.messages

        # 5) SALI STARTS A TASK and streams live ACTIVITY (§13). Create a real task + emit activity events.
        store = TaskStore(live_pool, pub)
        task = await store.create("Build a landing page", ["scaffold", "deploy"])
        await store.activate(task.id)
        await store.bind_workspace(task.id, objective="Build a landing page", explicit=None,
                                   sali_works_root=str(tmp_path))
        refetched = await store.get(task.id)  # re-fetch so workspace_root is populated
        assert refetched is not None and refetched.workspace_root is not None
        task = refetched
        workspace_root: str = refetched.workspace_root
        e_started = await pub.emit(event_type="task.started", task_id=task.id, subject_type="task",
                                   subject_id=task.id, origin="task", data={"objective": "Build a landing page"})
        await pub.emit(event_type="task.progress", task_id=task.id, subject_type="task",
                       subject_id=task.id, origin="task", data={"step": "scaffold"})
        watermark = e_started.sequence
        assert watermark is not None

        # 6) AGENT-INITIATED MESSAGE — Sali reaches out; a distinct channel, not a fake user turn (§10).
        await pub.emit(event_type="agent.message", origin="agent",
                       data={"channel": "agent_message", "importance": "discovery",
                             "text": "I found a faster framework for this."})

        # 7) APP BACKGROUNDS → DISCONNECT. The live socket is gone; the client remembers its last seq.
        #    (ws1 already returned — its scripted messages ended and it 'disconnected'.)
        last_seen = max((m["sequence"] for m in ws1.events() if m.get("sequence")), default=watermark)

        # 8) WORK CONTINUES WHILE OFFLINE: task completes, an artifact is produced, and the host gets hot.
        ws_root = Path(workspace_root)
        ws_root.mkdir(parents=True, exist_ok=True)
        artifact = ws_root / "index.html"
        artifact.write_text("<!doctype html><title>Landing</title>")
        await store.record_artifact(task.id, str(artifact), "created")
        await pub.emit(event_type="task.completed", task_id=task.id, subject_type="task",
                       subject_id=task.id, origin="task", data={"result": "deployed"})

        # 9) SYSTEM HEALTH becomes elevated → a PRESERVATION event streams (§18/§19, Prompt 12).
        from sali.tasks.incidents import ResourceIncidentStore
        await ResourceIncidentStore(live_pool, pub).record(
            kind="vram_pressure", severity="critical", workload="model",
            observed={"vram_used_pct": 94.0}, task_id=task.id)
        await pub.emit(event_type="resource.state_changed", origin="runtime",
                       data={"state": "critical", "preservation": True})

        # 10) RECONNECT → recover exactly the missed events (§29). Nothing before `last_seen` reappears.
        ws2 = FakeWebSocket(incoming=[{"type": "subscribe", "after_seq": last_seen}])
        await run_ws(ws2, access, live_pool)
        recovered = [m["event_type"] for m in ws2.events()]
        assert "task.completed" in recovered and "resource.state_changed" in recovered
        assert "task.started" not in recovered  # already seen → not replayed
        assert all(m["sequence"] > last_seen for m in ws2.events())

        # 11) USER RECEIVES THE TASK ARTIFACT (§9/§33) — metadata (no host path) then an authorized download.
        arts = (await c.get(f"/api/v1/tasks/{task.id}/artifacts", headers=auth(access))).json()
        assert arts and arts[0]["filename"] == "index.html" and "artifact_path" not in arts[0]
        dl = await c.get(arts[0]["download_url"], headers=auth(access))
        assert dl.status_code == 200 and "Landing" in dl.text

        # 12) USER INSPECTS THE TASK (§5/§14).
        detail = (await c.get(f"/api/v1/tasks/{task.id}", headers=auth(access))).json()
        assert detail["objective"] == "Build a landing page"

        # 13) The preservation incident is visible on the System screen (§18).
        incidents = (await c.get("/api/v1/resource-incidents", headers=auth(access))).json()
        assert any(i["kind"] == "vram_pressure" for i in incidents["incidents"])

        # 14) USER SENDS A NATURAL INSTRUCTION → state updates (§35).
        r = await c.post("/api/v1/conversation/message",
                         json={"content": "great, ship it"}, headers=auth(access))
        assert r.status_code == 200 and runtime.messages[-1] == "great, ship it"

        # 15) The whole time, this was ONE task with one identity, and it was never revoked.
        assert await RevocationStore(live_pool).is_revoked(task.id) is False


async def test_agent_message_carries_a_distinct_channel(live_pool: Any) -> None:
    # §10: agent-initiated messages are distinguishable at the transport/state level — they carry a
    # dedicated channel so the app renders them as Sali reaching out, not as a user's own message.
    _reset_manager()
    pub = EventPublisher(live_pool)
    await pub.emit(event_type="agent.message", origin="agent",
                   data={"channel": "agent_message", "importance": "completion",
                         "text": "Verification passed."})
    from sali.api.devices import DeviceStore
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None

    ws = FakeWebSocket(incoming=[{"type": "subscribe", "after_seq": 0}])
    await run_ws(ws, session.access_token, live_pool)
    agent_events = [m for m in ws.events() if m["event_type"] == "agent.message"]
    assert agent_events and agent_events[0]["data"]["channel"] == "agent_message"
    assert agent_events[0]["origin"] == "agent"
