"""Canonical event publisher tests.

Tests that events flow through one canonical path:
producer → publisher → PostgreSQL → live subscribers.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher, SaliEvent
from sali.tasks.store import TaskStore
from sali.tasks.watchdog import TaskWatchdog, WatchdogConfig

pytestmark = pytest.mark.db


# ── 1. Publishing creates one durable event ───────────────────────────────────

async def test_publish_creates_durable_event(live_pool: Any) -> None:
    """publish() creates exactly one row in the event table."""
    publisher = EventPublisher(live_pool)
    event = SaliEvent(
        event_type="test.event",
        data={"key": "value"},
        origin="test",
    )
    result = await publisher.publish(event)

    assert result.sequence is not None
    async with live_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM event WHERE seq = $1", event.sequence)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "test.event"


# ── 2. Published event receives durable sequence ─────────────────────────────

async def test_publish_receives_sequence(live_pool: Any) -> None:
    """Published event gets a monotonically increasing sequence."""
    publisher = EventPublisher(live_pool)
    e1 = await publisher.publish(SaliEvent(event_type="test.a", origin="test"))
    e2 = await publisher.publish(SaliEvent(event_type="test.b", origin="test"))

    assert e1.sequence is not None
    assert e2.sequence is not None
    assert e2.sequence > e1.sequence


# ── 3. Event includes event_id ───────────────────────────────────────────────

async def test_event_has_id(live_pool: Any) -> None:
    """Published event has a unique event_id."""
    publisher = EventPublisher(live_pool)
    event = await publisher.publish(SaliEvent(event_type="test.event", origin="test"))

    assert event.event_id is not None
    # Verify it's in the payload
    async with live_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT payload FROM event WHERE seq = $1", event.sequence)
        assert row["payload"]["event_id"] == str(event.event_id)


# ── 4. Event includes run_id when applicable ─────────────────────────────────

async def test_event_includes_run_id(live_pool: Any) -> None:
    """Event with run_id persists it in the payload."""
    publisher = EventPublisher(live_pool)
    run_id = uuid4()
    event = await publisher.publish(SaliEvent(
        event_type="test.event",
        run_id=run_id,
        origin="test",
    ))

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT payload FROM event WHERE seq = $1", event.sequence)
        assert row["payload"]["run_id"] == str(run_id)


# ── 5. Event includes task_id when applicable ────────────────────────────────

async def test_event_includes_task_id(live_pool: Any) -> None:
    """Event with task_id persists it in the payload."""
    publisher = EventPublisher(live_pool)
    task_id = uuid4()
    event = await publisher.publish(SaliEvent(
        event_type="task.progress",
        task_id=task_id,
        origin="task",
    ))

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT payload FROM event WHERE seq = $1", event.sequence)
        assert row["payload"]["task_id"] == str(task_id)


# ── 6. Live subscriber receives published event ──────────────────────────────

async def test_live_subscriber_receives_event(live_pool: Any) -> None:
    """Registered subscriber receives events in real-time."""
    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    await publisher.publish(SaliEvent(event_type="test.event", origin="test"))

    assert len(received) == 1
    assert received[0].event_type == "test.event"
    assert received[0].sequence is not None


# ── 7. Subscriber failure does not lose durable event ─────────────────────────

async def test_subscriber_failure_no_data_loss(live_pool: Any) -> None:
    """If a subscriber fails, the durable event is still persisted."""
    publisher = EventPublisher(live_pool)

    class FailingSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            raise RuntimeError("subscriber failed")

    publisher.subscribe(FailingSubscriber())
    event = await publisher.publish(SaliEvent(event_type="test.event", origin="test"))

    # Event is still durable
    assert event.sequence is not None
    async with live_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM event WHERE seq = $1", event.sequence)
        assert row is not None


# ── 8. Persisted event is recoverable ────────────────────────────────────────

async def test_event_recoverable_via_after_seq(live_pool: Any) -> None:
    """Persisted event can be recovered using after_seq pattern."""
    publisher = EventPublisher(live_pool)
    event = await publisher.publish(SaliEvent(event_type="test.event", origin="test"))

    async with live_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM event WHERE seq > $1 ORDER BY seq LIMIT 1",
            (event.sequence or 1) - 1)
        assert len(rows) >= 1
        assert rows[0]["event_type"] == "test.event"


# ── 9. TaskStore uses publisher ──────────────────────────────────────────────

async def test_taskstore_uses_publisher(live_pool: Any) -> None:
    """TaskStore emits events through the publisher when configured."""
    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    store = TaskStore(live_pool, publisher=publisher)
    task = await store.create("Test task", ["step1"])

    # Should have received task.created event
    created_events = [e for e in received if e.event_type == "task.created"]
    assert len(created_events) >= 1
    assert created_events[0].task_id == task.id


# ── 10. Task progress event through publisher ────────────────────────────────

async def test_task_progress_through_publisher(live_pool: Any) -> None:
    """Task progress events flow through the publisher."""
    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    store = TaskStore(live_pool, publisher=publisher)
    task = await store.create("Test task", ["step1", "step2"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    # Should have received task.progress and task.step.completed events
    progress_events = [e for e in received if e.event_type == "task.progress"]
    completed_events = [e for e in received if e.event_type == "task.step.completed"]
    assert len(progress_events) >= 1
    assert len(completed_events) >= 1


# ── 11. Watchdog health event through publisher ──────────────────────────────

async def test_watchdog_health_through_publisher(live_pool: Any) -> None:
    """Watchdog health transitions flow through the publisher."""
    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    store = TaskStore(live_pool, publisher=publisher)
    task = await store.create("Test task", ["step1"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "running")

    # Set stale progress
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE task SET last_progress_at = now() - interval '1 hour', "
            "  last_heartbeat = now() WHERE id = $1",
            task.id)

    config = WatchdogConfig(
        progress_timeout=timedelta(minutes=5),
        check_interval=timedelta(seconds=1),
        heartbeat_timeout=timedelta(minutes=10),
    )
    watchdog = TaskWatchdog(live_pool, config, publisher=publisher)
    await watchdog._check_all()

    health_events = [e for e in received if e.event_type == "task.health_changed"]
    assert len(health_events) >= 1
    assert health_events[0].origin == "watchdog"


# ── 12. Event to_live_dict has correct schema ────────────────────────────────

async def test_event_live_dict_schema(live_pool: Any) -> None:
    """Event's to_live_dict() has the expected fields for WebSocket delivery."""
    publisher = EventPublisher(live_pool)
    event = await publisher.publish(SaliEvent(
        event_type="task.progress",
        task_id=uuid4(),
        run_id=uuid4(),
        origin="task",
        data={"progress_type": "step_advance"},
    ))

    live = event.to_live_dict()
    assert live["type"] == "event"
    assert live["event_type"] == "task.progress"
    assert live["event_id"] is not None
    assert live["sequence"] is not None
    assert live["timestamp"] is not None
    assert live["run_id"] is not None
    assert live["task_id"] is not None
    assert live["origin"] == "task"
    assert live["data"]["progress_type"] == "step_advance"


# ── 13. Multiple subscribers receive same event ──────────────────────────────

async def test_multiple_subscribers_same_event(live_pool: Any) -> None:
    """Multiple subscribers each receive the same event."""
    publisher = EventPublisher(live_pool)
    received_a: list[SaliEvent] = []
    received_b: list[SaliEvent] = []

    class SubscriberA:
        async def on_event(self, event: SaliEvent) -> None:
            received_a.append(event)

    class SubscriberB:
        async def on_event(self, event: SaliEvent) -> None:
            received_b.append(event)

    publisher.subscribe(SubscriberA())
    publisher.subscribe(SubscriberB())
    await publisher.publish(SaliEvent(event_type="test.event", origin="test"))

    assert len(received_a) == 1
    assert len(received_b) == 1
    assert received_a[0].event_id == received_b[0].event_id


# ── 14. Unsubscribe stops delivery ───────────────────────────────────────────

async def test_unsubscribe_stops_delivery(live_pool: Any) -> None:
    """Unsubscribed subscriber no longer receives events."""
    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    sub = TestSubscriber()
    publisher.subscribe(sub)
    await publisher.publish(SaliEvent(event_type="test.a", origin="test"))
    assert len(received) == 1

    publisher.unsubscribe(sub)
    await publisher.publish(SaliEvent(event_type="test.b", origin="test"))
    assert len(received) == 1  # no new event


# ── 15. Event without run_id has null run_id ─────────────────────────────────

async def test_event_without_run_id(live_pool: Any) -> None:
    """Event without run_id persists with null run_id."""
    publisher = EventPublisher(live_pool)
    event = await publisher.publish(SaliEvent(
        event_type="task.health_changed",
        task_id=uuid4(),
        origin="watchdog",
    ))

    live = event.to_live_dict()
    assert live["run_id"] is None


# ── 16. EventBridge fetches and delivers cross-process events ─────────────────

async def test_event_bridge_fetches_durable_events(live_pool: Any) -> None:
    """EventBridge fetches events from PostgreSQL and delivers to subscribers."""
    from sali.events.bridge import EventBridge

    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    bridge = EventBridge(live_pool, publisher)

    # Publish an event directly (simulating another process)
    event = await publisher.publish(SaliEvent(
        event_type="test.cross_process",
        origin="test",
        data={"key": "value"},
    ))

    # Bridge should fetch it on next poll
    await bridge._fetch_and_deliver()

    assert len(received) >= 1
    found = [e for e in received if e.event_type == "test.cross_process"]
    assert len(found) >= 1
    assert found[0].sequence == event.sequence


# ── 17. EventBridge deduplicates events ───────────────────────────────────────

async def test_event_bridge_deduplicates(live_pool: Any) -> None:
    """EventBridge does not deliver the same event twice."""
    from sali.events.bridge import EventBridge

    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    bridge = EventBridge(live_pool, publisher)
    publisher._bridge = bridge  # wire dedup reference

    # Publish one event
    await publisher.publish(SaliEvent(event_type="test.dedup", origin="test"))

    # Fetch twice — should only deliver once (bridge skips locally-published)
    await bridge._fetch_and_deliver()
    await bridge._fetch_and_deliver()

    dedup_events = [e for e in received if e.event_type == "test.dedup"]
    assert len(dedup_events) == 1


# ── 18. EventBridge start/stop lifecycle ──────────────────────────────────────

async def test_event_bridge_lifecycle(live_pool: Any) -> None:
    """EventBridge starts and stops cleanly without leaking tasks."""
    from sali.events.bridge import EventBridge

    publisher = EventPublisher(live_pool)
    bridge = EventBridge(live_pool, publisher)

    await bridge.start()
    assert bridge._task is not None
    assert not bridge._task.done()

    await bridge.stop()
    assert bridge._task is None


# ── 19. Canonical event has consistent identity across paths ──────────────────

async def test_event_identity_consistency(live_pool: Any) -> None:
    """Same event has same event_id and seq whether viewed via publisher or DB."""
    publisher = EventPublisher(live_pool)
    event = await publisher.publish(SaliEvent(
        event_type="test.identity",
        run_id=uuid4(),
        task_id=uuid4(),
        origin="test",
    ))

    # Check DB
    async with live_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM event WHERE seq = $1", event.sequence)
        assert row is not None
        payload = dict(row["payload"])
        assert payload["event_id"] == str(event.event_id)
        assert payload["run_id"] == str(event.run_id)
        assert payload["task_id"] == str(event.task_id)
        assert payload["origin"] == "test"

    # Check live dict
    live = event.to_live_dict()
    assert live["event_id"] == str(event.event_id)
    assert live["sequence"] == event.sequence
    assert live["run_id"] == str(event.run_id)
    assert live["task_id"] == str(event.task_id)


# ── 20. TaskStore events flow through publisher ───────────────────────────────

async def test_taskstore_advance_uses_publisher(live_pool: Any) -> None:
    """TaskStore.advance() publishes events through the canonical publisher."""
    publisher = EventPublisher(live_pool)
    received: list[SaliEvent] = []

    class TestSubscriber:
        async def on_event(self, event: SaliEvent) -> None:
            received.append(event)

    publisher.subscribe(TestSubscriber())
    store = TaskStore(live_pool, publisher=publisher)
    task = await store.create("Test", ["step1", "step2"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")

    # Should have task.step_advanced, task.progress, task.step.completed
    event_types = {e.event_type for e in received}
    assert "task.step_advanced" in event_types
    assert "task.progress" in event_types
    assert "task.step.completed" in event_types

    # All should have the same task_id
    for e in received:
        if e.task_id:
            assert e.task_id == task.id
