"""Turn 5 of the Work-audit: backend piece.

  1. POST /api/v1/tasks/{id}/cancel now routes through revoke_intent (matches /abandon,
     which Turn 2 already made the tombstone+propagation path). A cancelled task is dead;
     the mobile Cancel button is the same authoritative revocation as Abandon.
  2. GET /api/v1/tasks/{id}/events is a new task-scoped durable-log route so the iOS
     pulse seed on TaskDetailView doesn't have to filter a global 200-row window
     client-side and miss events on a busy backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.events.publisher import EventPublisher
from sali.tasks.revocation import RevocationStore
from sali.tasks.store import TaskStore
from tests.apiutil import RuntimeStub, auth, build_app, client, enroll

pytestmark = pytest.mark.db


async def _active_task(pool: Any, objective: str = "Test task") -> Any:
    store = TaskStore(pool, EventPublisher(pool))
    task = await store.create(objective, ["step 1", "step 2"])
    await store.activate(task.id)
    return task


# ── (1) /cancel now = revoke_intent (Turn 2's tombstone-write path) ─────────────

async def test_cancel_writes_tombstone_like_abandon(live_pool: Any) -> None:
    """POST /cancel now writes a revoked_intent row (was: bare store.cancel()). The
    revoked task is unresumable, identical shape to what /abandon produces."""
    session = await enroll(live_pool, role="controller")
    task = await _active_task(live_pool, "Cancel me")
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r = await c.post(f"/api/v1/tasks/{task.id}/cancel", headers=auth(session))
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    # A durable tombstone landed - identical semantics to /abandon (Turn 2).
    assert await RevocationStore(live_pool).is_revoked(task.id) is True


async def test_cancel_and_abandon_produce_same_terminal_state(live_pool: Any) -> None:
    """After Turn 5, /cancel and /abandon converge on revoke_intent. Two tasks, one
    cancelled and one abandoned, must end in the same durable shape."""
    session = await enroll(live_pool, role="owner")
    cancelled = await _active_task(live_pool, "Cancel me too")
    abandoned = await _active_task(live_pool, "Abandon me")
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r1 = await c.post(f"/api/v1/tasks/{cancelled.id}/cancel", headers=auth(session))
        r2 = await c.post(f"/api/v1/tasks/{abandoned.id}/abandon", headers=auth(session))
        assert r1.status_code == 200
        assert r2.status_code == 200

    revocation = RevocationStore(live_pool)
    assert await revocation.is_revoked(cancelled.id)
    assert await revocation.is_revoked(abandoned.id)

    # Both should have the same status in the task table (both revoke_intent → 'abandoned')
    async with live_pool.acquire() as c:
        s1 = await c.fetchval("SELECT status FROM sali.task WHERE id=$1", cancelled.id)
        s2 = await c.fetchval("SELECT status FROM sali.task WHERE id=$1", abandoned.id)
    assert s1 == s2 == "abandoned"


# ── (2) task-scoped /tasks/{id}/events endpoint ─────────────────────────────────

async def test_task_events_filters_to_task(live_pool: Any) -> None:
    """The route returns ONLY events for the given task via UNION on subject_id AND
    payload->>'task_id'. Events from other tasks or other subject types are excluded."""
    session = await enroll(live_pool, role="observer")   # read-only is enough
    task_a = await _active_task(live_pool, "Task A")
    task_b = await _active_task(live_pool, "Task B")

    # Emit three specific events: two on task_a (one direct-subject, one payload-carried),
    # one on task_b (a decoy).
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.event (event_type, subject_type, subject_id, payload) "
            "VALUES ($1, 'task', $2, $3)",
            "turn5.subject_direct", task_a.id, {"note": "direct on A"})
        await c.execute(
            "INSERT INTO sali.event (event_type, subject_type, subject_id, payload) "
            "VALUES ($1, 'commitment', NULL, $2)",
            "turn5.payload_carried",
            {"task_id": str(task_a.id), "note": "commitment linked to A"})
        await c.execute(
            "INSERT INTO sali.event (event_type, subject_type, subject_id, payload) "
            "VALUES ($1, 'task', $2, $3)",
            "turn5.decoy_on_b", task_b.id, {"note": "decoy on B"})

    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r = await c.get(f"/api/v1/tasks/{task_a.id}/events", headers=auth(session))
        assert r.status_code == 200
        rows = r.json()

    types = {row["type"] for row in rows}
    assert "turn5.subject_direct" in types
    assert "turn5.payload_carried" in types, (
        "task-scoped events must include rows carrying task_id in payload "
        "(commitments, obligations, side_effects)")
    assert "turn5.decoy_on_b" not in types


async def test_task_events_after_seq_gap_fills(live_pool: Any) -> None:
    """?after_seq=N returns only rows with seq > N, so the client's pulse watermark
    can gap-fill without re-scanning the whole tail."""
    session = await enroll(live_pool, role="observer")
    task = await _active_task(live_pool, "Gap-fill task")

    async with live_pool.acquire() as c:
        # Baseline events - grab their seq numbers.
        await c.execute(
            "INSERT INTO sali.event (event_type, subject_type, subject_id, payload) "
            "VALUES ('turn5.event_1', 'task', $1, '{}'::jsonb)", task.id)
        first_seq = await c.fetchval(
            "SELECT max(seq) FROM sali.event WHERE subject_id = $1", task.id)
        assert first_seq is not None

        # Later event.
        await c.execute(
            "INSERT INTO sali.event (event_type, subject_type, subject_id, payload) "
            "VALUES ('turn5.event_2', 'task', $1, '{}'::jsonb)", task.id)

    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r = await c.get(
            f"/api/v1/tasks/{task.id}/events?after_seq={first_seq}",
            headers=auth(session))
        assert r.status_code == 200
        rows = r.json()

    types = [row["type"] for row in rows]
    assert types == ["turn5.event_2"], (
        f"after_seq must skip already-seen rows, got: {types}")


async def test_task_events_limit_bounds_response(live_pool: Any) -> None:
    """?limit=N is honored (bounded 1..500)."""
    session = await enroll(live_pool, role="observer")
    task = await _active_task(live_pool, "Bounded task")

    async with live_pool.acquire() as c:
        for i in range(10):
            await c.execute(
                "INSERT INTO sali.event (event_type, subject_type, subject_id, payload) "
                "VALUES ($1, 'task', $2, '{}'::jsonb)",
                f"turn5.batch_{i}", task.id)

    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r = await c.get(f"/api/v1/tasks/{task.id}/events?limit=3", headers=auth(session))
        assert r.status_code == 200
        assert len(r.json()) == 3
