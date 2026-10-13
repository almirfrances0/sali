"""MessageInbox — the durable attention queue (Prompt 1).

A message Almir sends while Sali is busy is persisted here so it survives a crash/restart, carries a
deterministic classification + priority, is claimed EXACTLY ONCE (FOR UPDATE SKIP LOCKED — no duplicate
processing), and is served in priority-then-arrival order. Distinct from the conversation transcript
(`message`): this is the queue of things-to-attend-to, not the chat log. Lifecycle events flow through
the canonical EventPublisher, never a second pipeline.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class InboxMessage:
    id: UUID
    content: str
    origin: str
    priority: str
    classification: str | None
    related_task_id: UUID | None
    status: str
    session_id: UUID | None
    created_at: Any


def _row(r: Any) -> InboxMessage:
    return InboxMessage(
        id=r["id"], content=r["content"], origin=r["origin"], priority=r["priority"],
        classification=r["classification"], related_task_id=r["related_task_id"], status=r["status"],
        session_id=r["session_id"], created_at=r["created_at"])


class MessageInbox:
    """Durable persistence + exactly-once claiming for incoming attention messages."""

    def __init__(self, pool: Any, *, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def enqueue(
        self, content: str, *, session_id: UUID | None = None, origin: str = "cli",
        priority: str = "normal", classification: str | None = None,
        related_task_id: UUID | None = None, dedup_key: str | None = None,
    ) -> InboxMessage | None:
        """Persist an incoming message. If ``dedup_key`` is already queued, this is a no-op (returns None)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO incoming_message "
                "  (session_id, content, origin, priority, classification, related_task_id, dedup_key) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING RETURNING *",
                session_id, content, origin, priority, classification, related_task_id, dedup_key)
        if row is None:
            return None  # duplicate — already queued, never processed twice
        msg = _row(row)
        await self._emit("message.queued", msg)
        return msg

    async def claim_next(self) -> InboxMessage | None:
        """Atomically claim the highest-priority pending message (priority, then FIFO). FOR UPDATE SKIP
        LOCKED so two concurrent claimers never take the same message. Returns None if the queue is empty."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE incoming_message SET status='processing', claimed_at=now() "
                "WHERE id = (SELECT id FROM incoming_message WHERE status='pending' "
                "  ORDER BY priority_rank(priority) DESC, created_at ASC "
                "  FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *")
        if row is None:
            return None
        msg = _row(row)
        await self._emit("message.dequeued", msg)
        return msg

    async def claim_next_queued(self) -> InboxMessage | None:
        """Claim the oldest QUEUE_FOR_LATER message (the after-you-finish queue) — same exactly-once
        contract as claim_next, but scoped to deferred messages so it can never steal a message the
        live routing path is about to handle inline (audit: these rows were persisted, classified,
        and then never acted on by anything)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE incoming_message SET status='processing', claimed_at=now() "
                "WHERE id = (SELECT id FROM incoming_message WHERE status='pending' "
                "  AND classification='queue_for_later' "
                "  ORDER BY priority_rank(priority) DESC, created_at ASC "
                "  FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *")
        if row is None:
            return None
        msg = _row(row)
        await self._emit("message.dequeued", msg)
        return msg

    async def complete(
        self, message_id: UUID, *, run_id: UUID | None = None, classification: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE incoming_message SET status='completed', processed_at=now(), "
                "  run_id=coalesce($2,run_id), classification=coalesce($3,classification) WHERE id=$1",
                message_id, run_id, classification)

    async def defer(self, message_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE incoming_message SET status='deferred' WHERE id=$1", message_id)

    async def cancel(self, message_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE incoming_message SET status='cancelled' WHERE id=$1", message_id)

    async def get(self, message_id: UUID) -> InboxMessage | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM incoming_message WHERE id=$1", message_id)
        return _row(row) if row is not None else None

    async def pending(self, *, limit: int = 50) -> list[InboxMessage]:
        """The queued-but-unprocessed messages, priority-then-arrival ordered (used for recovery + status)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM incoming_message WHERE status='pending' "
                "ORDER BY priority_rank(priority) DESC, created_at ASC LIMIT $1", limit)
        return [_row(r) for r in rows]

    async def reclaim_stale(self, *, older_than_s: int = 120) -> int:
        """Messages left 'processing' by a crashed process (claimed but never completed) → back to
        'pending' so they are handled after a restart, never silently dropped (§14 recovery)."""
        async with self._pool.acquire() as conn:
            n = await conn.fetchval(
                "WITH r AS (UPDATE incoming_message SET status='pending', claimed_at=NULL "
                "  WHERE status='processing' AND claimed_at < now() - ($1||' seconds')::interval "
                "  RETURNING 1) SELECT count(*) FROM r", str(older_than_s))
        return int(n or 0)

    async def _emit(self, event_type: str, msg: InboxMessage) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):  # events are best-effort; never block the queue
            await self._publisher.emit(
                event_type=event_type, task_id=msg.related_task_id, session_id=msg.session_id,
                subject_type="message", subject_id=msg.id, origin="attention",
                data={"message_id": str(msg.id), "priority": msg.priority,
                      "classification": msg.classification, "content_preview": msg.content[:100]})
