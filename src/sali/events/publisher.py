"""Canonical event publisher — single path for all Sali events.

Every important event flows through this publisher:
1. Normalize into a SaliEvent
2. Persist durably to PostgreSQL event table
3. Obtain durable sequence (auto-increment ID)
4. Notify live subscribers via EventBus (PostgreSQL LISTEN/NOTIFY)
5. Local subscribers (WebSocket, terminal) receive events

This replaces the pattern where components wrote directly to the event table
AND separately broadcast to WebSocket, creating duplicate paths.

Architecture:
    Producer → EventPublisher.publish()
                  ├── PostgreSQL event table (durable)
                  ├── EventBus notification (cross-process)
                  └── Local subscribers (WebSocket, terminal)
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.events.publisher")


class EventSubscriber(Protocol):
    """Protocol for live event subscribers."""
    async def on_event(self, event: SaliEvent) -> None: ...


@dataclass(slots=True)
class SaliEvent:
    """A canonical Sali event with full identity."""
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    event_id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_id: UUID | None = None
    task_id: UUID | None = None
    session_id: UUID | None = None
    origin: str = "system"  # agent | tool | task | watchdog | system | background
    subject_type: str | None = None
    subject_id: UUID | None = None
    # Durable sequence — set after persistence, None before
    sequence: int | None = None

    def to_db_payload(self) -> dict[str, Any]:
        """Convert to the payload stored in the event table."""
        return {
            "event_id": str(self.event_id),
            "run_id": str(self.run_id) if self.run_id else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "origin": self.origin,
            **self.data,
        }

    def to_live_dict(self) -> dict[str, Any]:
        """Convert to the dict sent to live subscribers (WebSocket, terminal)."""
        return {
            "type": "event",
            "event_type": self.event_type,
            "event_id": str(self.event_id),
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "run_id": str(self.run_id) if self.run_id else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "origin": self.origin,
            "data": self.data,
        }


class EventPublisher:
    """Canonical event publisher — single path for all Sali events.

    Usage:
        publisher = EventPublisher(pool)
        await publisher.publish(SaliEvent(
            event_type="task.progress",
            task_id=task_id,
            origin="task",
            data={"progress_type": "step_advance"},
        ))
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._subscribers: list[EventSubscriber] = []
        self._ws_manager: Any = None  # set by runtime
        self._bridge: Any = None  # EventBridge — set by runtime

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Register a live subscriber."""
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        """Unregister a live subscriber."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(subscriber)

    def set_ws_manager(self, manager: Any) -> None:
        """Set the WebSocket connection manager for live delivery."""
        self._ws_manager = manager

    async def publish(self, event: SaliEvent) -> SaliEvent:
        """Publish an event: persist durably, then deliver to live subscribers.

        Returns the event with its durable sequence set.
        """
        # Ephemeral streaming tokens bypass database persistence to eliminate latency
        if event.event_type in ("agent.token", "agent.thinking"):
            await self._deliver_live(event)
            return event

        # 1. Persist durably
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                "VALUES ($1, $2, $3, $4) RETURNING seq, id",
                event.event_type,
                event.subject_type or _infer_subject_type(event),
                event.subject_id or event.task_id,
                event.to_db_payload(),
            )
            event.sequence = row["seq"]

        # Mark as locally published so the bridge doesn't re-deliver
        if self._bridge is not None and event.sequence is not None:
            self._bridge.mark_local(event.sequence)

        # 2. Deliver to live subscribers (best-effort — never break persistence)
        await self._deliver_live(event)

        return event

    async def emit(
        self, event_type: str, *, task_id: UUID | None = None, run_id: UUID | None = None,
        session_id: UUID | None = None, subject_type: str | None = None,
        subject_id: UUID | None = None, origin: str = "system", data: dict[str, Any] | None = None,
    ) -> SaliEvent:
        """Construct + publish a SaliEvent from keyword fields. Lets callers in other layers (tasks,
        watchdog) emit events WITHOUT importing SaliEvent — keeping the layering contract intact."""
        return await self.publish(SaliEvent(
            event_type=event_type, task_id=task_id, run_id=run_id, session_id=session_id,
            subject_type=subject_type, subject_id=subject_id, origin=origin, data=data or {}))

    async def publish_on_conn(self, conn: Any, event: SaliEvent) -> SaliEvent:
        """Persist an event using an existing connection so it participates in the caller's transaction.

        IMPORTANT: this NO LONGER broadcasts live. Broadcasting inside the caller's transaction
        would show clients an event that a subsequent rollback erases from the durable log — a
        phantom event. Callers who need live delivery must invoke `publish_committed(event)` AFTER
        their transaction commits. Callers who don't need live delivery (durable-only writes) can
        just call this and stop.
        """
        row = await conn.fetchrow(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ($1, $2, $3, $4) RETURNING seq, id",
            event.event_type,
            event.subject_type or _infer_subject_type(event),
            event.subject_id or event.task_id,
            event.to_db_payload(),
        )
        event.sequence = row["seq"]
        # Mark as locally published so the bridge doesn't re-deliver AFTER commit fires NOTIFY.
        if self._bridge is not None and event.sequence is not None:
            self._bridge.mark_local(event.sequence)
        return event

    async def publish_committed(self, event: SaliEvent) -> None:
        """Broadcast an event to live subscribers AFTER the caller's transaction has committed.

        The pair (publish_on_conn INSIDE tx, publish_committed AFTER commit) is the transaction-
        safe pattern. If the tx rolls back, the caller simply never calls publish_committed and
        no phantom event reaches WebSocket clients. Rolled-back rows are already gone from the
        durable log; the mark_local set is bounded so a rolled-back seq eventually falls off.
        """
        if event.sequence is None:
            log.warning("publish_committed_missing_sequence", event_type=event.event_type)
        await self._deliver_live(event)

    async def _deliver_live(self, event: SaliEvent) -> None:
        """Deliver event to all live subscribers."""
        live_dict = event.to_live_dict()

        # WebSocket clients
        if self._ws_manager is not None:
            with contextlib.suppress(Exception):
                await self._ws_manager.broadcast(live_dict)

        # Local subscribers (terminal callbacks, etc.)
        for subscriber in self._subscribers:
            with contextlib.suppress(Exception):
                await subscriber.on_event(event)


def _infer_subject_type(event: SaliEvent) -> str | None:
    """Infer subject_type from event_type prefix."""
    if event.event_type.startswith("task."):
        return "task"
    if event.event_type.startswith("tool."):
        return "tool"
    if event.event_type.startswith("memory."):
        return "memory"
    if event.event_type.startswith("schedule."):
        return "schedule"
    return None
