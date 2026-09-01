"""WebSocket authentication + event-sequence recovery (Prompt 13 §25/§29/§43).

The handler is exercised directly with a fake WebSocket so it runs on the same event loop as `live_pool`
(httpx's ASGI transport doesn't speak WebSocket). Proves:
- an unauthenticated connection is closed immediately (never accepted)
- an authenticated connection accepts and, on subscribe(after_seq=N), replays every durable event with
  seq > N — closing the gap opened by a disconnect
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.api.ws import manager
from sali.events.publisher import EventPublisher
from tests.apiutil import FakeWebSocket, enroll, run_ws

pytestmark = pytest.mark.db


def _reset_manager() -> None:
    manager._connections.clear()
    manager._last_activity.clear()
    manager._last_event_seq.clear()


async def test_unauthenticated_ws_is_rejected(live_pool: Any) -> None:
    _reset_manager()
    ws = FakeWebSocket(headers={})
    await run_ws(ws, None, live_pool)
    assert ws.accepted is False and ws.closed_code == 4001  # closed unauthorized, never accepted


async def test_invalid_token_ws_is_rejected(live_pool: Any) -> None:
    _reset_manager()
    ws = FakeWebSocket()
    await run_ws(ws, "bogus", live_pool)
    assert ws.accepted is False and ws.closed_code == 4001


async def test_authenticated_ws_accepts_and_replays_missed_events(live_pool: Any) -> None:
    _reset_manager()
    session = await enroll(live_pool, role="owner")

    # Emit three durable events; capture the seq of the first so we can "miss" the last two.
    pub = EventPublisher(live_pool)
    e1 = await pub.emit(event_type="task.started", subject_type="task", subject_id=uuid4(),
                        origin="task", data={"n": 1})
    await pub.emit(event_type="task.progress", subject_type="task", subject_id=uuid4(),
                   origin="task", data={"n": 2})
    await pub.emit(event_type="task.completed", subject_type="task", subject_id=uuid4(),
                   origin="task", data={"n": 3})
    baseline = e1.sequence
    assert baseline is not None

    # Client reconnects knowing only up to `baseline`, then subscribes to recover the rest (§29).
    ws = FakeWebSocket(incoming=[{"type": "subscribe", "after_seq": baseline}])
    await run_ws(ws, session.access_token, live_pool)

    assert ws.accepted is True and ws.closed_code is None
    replayed = [m for m in ws.sent if m.get("type") == "event"]
    ns = [m["data"]["n"] for m in replayed]
    assert ns == [2, 3]  # exactly the two events after the client's watermark, in order
    assert all(m["sequence"] > baseline for m in replayed)
    subscribed = next(m for m in ws.sent if m.get("type") == "subscribed")
    assert subscribed["replayed"] == 2


async def test_subscribe_without_gap_replays_nothing(live_pool: Any) -> None:
    _reset_manager()
    session = await enroll(live_pool, role="owner")
    pub = EventPublisher(live_pool)
    e = await pub.emit(event_type="task.started", subject_type="task", subject_id=uuid4(),
                       origin="task", data={"n": 1})
    assert e.sequence is not None

    # Client is already caught up (after_seq == latest) → no replay.
    ws = FakeWebSocket(incoming=[{"type": "subscribe", "after_seq": e.sequence}])
    await run_ws(ws, session.access_token, live_pool)
    assert not [m for m in ws.sent if m.get("type") == "event"]
    subscribed = next(m for m in ws.sent if m.get("type") == "subscribed")
    assert subscribed["replayed"] == 0


async def test_ping_pong(live_pool: Any) -> None:
    _reset_manager()
    session = await enroll(live_pool, role="owner")
    ws = FakeWebSocket(incoming=[{"type": "ping"}])
    await run_ws(ws, session.access_token, live_pool)
    assert any(m.get("type") == "pong" for m in ws.sent)
