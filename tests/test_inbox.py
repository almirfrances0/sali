"""MessageInbox (Prompt 1): the durable attention queue — messages survive restart, are claimed exactly
once in priority-then-arrival order, dedup by key, recover after a crash, and emit canonical events."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher, SaliEvent
from sali.tasks.inbox import MessageInbox

pytestmark = pytest.mark.db


async def test_enqueue_persists_and_survives_restart(live_pool: Any) -> None:
    session = uuid4()
    inbox = MessageInbox(live_pool)
    msg = await inbox.enqueue("zip project.zip and send it", session_id=session, origin="ios", priority="high")
    assert msg is not None and msg.status == "pending" and msg.priority == "high"
    # a FRESH inbox (simulated restart) still sees it — it lives in the DB, not memory (§8/§14)
    reborn = MessageInbox(live_pool)
    pend = await reborn.pending()
    assert any(m.id == msg.id and m.content == "zip project.zip and send it" for m in pend)


async def test_claimed_in_priority_then_fifo_order(live_pool: Any) -> None:
    inbox = MessageInbox(live_pool)
    await inbox.enqueue("normal one")
    await inbox.enqueue("low one", priority="low")
    await inbox.enqueue("urgent one", priority="urgent")
    await inbox.enqueue("high one", priority="high")
    await inbox.enqueue("normal two")  # same priority as "normal one" → FIFO after it
    order = []
    while (m := await inbox.claim_next()) is not None:
        order.append(m.content)
    assert order == ["urgent one", "high one", "normal one", "normal two", "low one"]


async def test_claim_is_exactly_once(live_pool: Any) -> None:
    inbox = MessageInbox(live_pool)
    m = await inbox.enqueue("do the thing")
    assert m is not None
    first = await inbox.claim_next()
    assert first is not None and first.id == m.id and first.status == "processing"
    # once claimed it is no longer pending — never handed out twice
    assert await inbox.claim_next() is None
    got = await inbox.get(m.id)
    assert got is not None and got.status == "processing"


async def test_dedup_key_prevents_duplicate_enqueue(live_pool: Any) -> None:
    inbox = MessageInbox(live_pool)
    a = await inbox.enqueue("send report", dedup_key="report-2026-08-31")
    b = await inbox.enqueue("send report", dedup_key="report-2026-08-31")  # same key → no-op
    assert a is not None and b is None
    assert len(await inbox.pending()) == 1


async def test_complete_defer_cancel_transitions(live_pool: Any) -> None:
    inbox = MessageInbox(live_pool)
    m1 = await inbox.enqueue("one")
    m2 = await inbox.enqueue("two")
    m3 = await inbox.enqueue("three")
    assert m1 and m2 and m3
    run = uuid4()
    await inbox.complete(m1.id, run_id=run, classification="quick_action")
    await inbox.defer(m2.id)
    await inbox.cancel(m3.id)
    g1, g2, g3 = await inbox.get(m1.id), await inbox.get(m2.id), await inbox.get(m3.id)
    assert g1 is not None and g1.status == "completed"
    assert g2 is not None and g2.status == "deferred"
    assert g3 is not None and g3.status == "cancelled"
    assert await inbox.pending() == []  # none left pending


async def test_reclaim_stale_after_crash(live_pool: Any) -> None:
    inbox = MessageInbox(live_pool)
    m = await inbox.enqueue("interrupted work")
    assert m is not None
    claimed = await inbox.claim_next()  # a process claims it → 'processing'
    assert claimed is not None
    # that process crashes without completing → the message is stuck 'processing'. Recovery re-queues it.
    n = await inbox.reclaim_stale(older_than_s=0)
    assert n == 1
    again = await inbox.claim_next()  # now claimable again — never silently lost
    assert again is not None and again.id == m.id


async def test_queue_events_use_the_canonical_publisher(live_pool: Any) -> None:
    received: list[SaliEvent] = []

    class Sub:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher = EventPublisher(live_pool)
    publisher.subscribe(Sub())
    inbox = MessageInbox(live_pool, publisher=publisher)
    m = await inbox.enqueue("ping", priority="high")
    assert m is not None
    await inbox.claim_next()
    kinds = [e.event_type for e in received]
    assert "message.queued" in kinds and "message.dequeued" in kinds
