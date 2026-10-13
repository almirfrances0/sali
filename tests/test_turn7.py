"""Turn 7 of the Work-audit: one vocabulary for step completion.

Closes five gaps:
  1. task.step.* events now carry {done, total, verified, skipped, task_status}.
  2. Auto-complete predicate consumes the same tally the events carry.
  3. Skipped counts as settled (backend + iOS agree).
  4. Parent step auto-closes when all children settle (cascade up the chain).
  5. Bare-store inline_done emits task.finished (was: only agent.message).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.tasks.store import TaskStore, _compute_tally, _parent_auto_close

pytestmark = pytest.mark.db


async def _events_since(pool, task_id, seq_before):
    async with pool.acquire() as c:
        return await c.fetch(
            "SELECT event_type, payload FROM sali.event "
            "WHERE subject_id = $1 AND seq > $2 ORDER BY seq",
            task_id, seq_before)


async def _seq_now(pool, task_id):
    async with pool.acquire() as c:
        return await c.fetchval(
            "SELECT coalesce(max(seq), 0) FROM sali.event WHERE subject_id = $1",
            task_id) or 0


# ── (1) _compute_tally — the single source of truth ─────────────────────────────

async def test_compute_tally_shape(live_pool: Any) -> None:
    """Baseline: {done, total, verified, skipped, task_status} with the right rules."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test", ["a", "b", "c", "d"])

    async with live_pool.acquire() as c:
        # Set: 1=done+verified, 2=done (no verify), 3=skipped, 4=pending
        await c.execute("UPDATE sali.task_step SET status='done', verified_by=$3 "
                        "WHERE task_id=$1 AND seq=$2", task.id, 1, uuid4())
        await c.execute("UPDATE sali.task_step SET status='done' "
                        "WHERE task_id=$1 AND seq=$2", task.id, 2)
        await c.execute("UPDATE sali.task_step SET status='skipped' "
                        "WHERE task_id=$1 AND seq=$2", task.id, 3)
        tally = await _compute_tally(c, task.id)

    assert tally == {"done": 2, "total": 4, "verified": 1, "skipped": 1,
                     "task_status": "running"}, tally


async def test_compute_tally_auto_completes_on_done_plus_skipped(live_pool: Any) -> None:
    """A task with 3 done + 2 skipped MUST auto-complete (task_status='done')."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test", ["a", "b", "c", "d", "e"])

    async with live_pool.acquire() as c:
        await c.execute("UPDATE sali.task_step SET status='done' "
                        "WHERE task_id=$1 AND seq <= 3", task.id)
        await c.execute("UPDATE sali.task_step SET status='skipped' "
                        "WHERE task_id=$1 AND seq > 3", task.id)
        tally = await _compute_tally(c, task.id)

    assert tally["task_status"] == "done"
    assert tally["done"] == 3
    assert tally["skipped"] == 2
    assert tally["total"] == 5


async def test_compute_tally_empty_task_stays_open(live_pool: Any) -> None:
    """A task with zero steps must NOT auto-complete (preserves pre-Turn-7 contract)."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Empty task", [])
    async with live_pool.acquire() as c:
        tally = await _compute_tally(c, task.id)
    assert tally["total"] == 0
    assert tally["task_status"] == "running"


# ── (2) Events carry tally + auto-complete uses same numbers ────────────────────

async def test_step_events_carry_tally(live_pool: Any) -> None:
    """Every task.step.* event fired by advance() now carries the full tally."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Test emit", ["step 1", "step 2"])
    await store.activate(task.id)
    seq_before = await _seq_now(live_pool, task.id)

    await store.advance(task.id, 1, "done")

    events = await _events_since(live_pool, task.id, seq_before)
    step_events = [e for e in events if e["event_type"] in
                   ("task.step_advanced", "task.progress", "task.step.completed")]
    assert step_events, f"expected step-family events, got: {[e['event_type'] for e in events]}"
    # Every one of them MUST carry the four tally fields.
    for e in step_events:
        payload = dict(e["payload"])
        for key in ("done", "total", "verified", "skipped", "task_status"):
            assert key in payload, (
                f"{e['event_type']} payload missing {key!r}; keys: {list(payload)}")
        assert payload["total"] == 2
        assert payload["done"] == 1
        assert payload["task_status"] == "running"


# ── (3) skipped counts as settled — auto-complete fires ─────────────────────────

async def test_auto_complete_treats_skipped_as_settled(live_pool: Any) -> None:
    """The classic scenario: 3 done + 2 skipped → auto-complete + task.finished."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Mixed task", ["a", "b", "c", "d", "e"])
    await store.activate(task.id)
    seq_before = await _seq_now(live_pool, task.id)

    await store.advance(task.id, 1, "done")
    await store.advance(task.id, 2, "done")
    await store.advance(task.id, 3, "done")
    await store.advance(task.id, 4, "skipped")
    await store.advance(task.id, 5, "skipped")   # last step — triggers auto-complete

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT status FROM sali.task WHERE id=$1", task.id)
    assert row["status"] == "done", "auto-complete must fire on 3 done + 2 skipped"

    events = await _events_since(live_pool, task.id, seq_before)
    finished = [e for e in events if e["event_type"] == "task.finished"]
    assert finished, (
        "bare-store inline_done MUST emit task.finished (Turn 7 - was missing)")
    payload = dict(finished[-1]["payload"])
    assert payload.get("status") == "done"
    assert payload.get("via") == "auto"


# ── (4) parent auto-close cascade ───────────────────────────────────────────────

async def test_parent_auto_closes_when_all_children_settle(live_pool: Any) -> None:
    """Sub-step scenario: parent seq=1, children seq=2 (parent_seq=1), seq=3 (parent_seq=1).
    When both children settle, the parent must auto-close via the cascade."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Parent task", ["parent step"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:
        # Add two children of seq=1.
        await c.execute(
            "INSERT INTO sali.task_step (task_id, seq, description, parent_seq) "
            "VALUES ($1, 2, 'child A', 1), ($1, 3, 'child B', 1)", task.id)
        # Sanity: parent still pending
        p = await c.fetchval(
            "SELECT status FROM sali.task_step WHERE task_id=$1 AND seq=1", task.id)
    assert p == "pending"

    # Advance first child - parent should NOT yet close (one sibling still pending).
    await store.advance(task.id, 2, "done")
    async with live_pool.acquire() as c:
        p = await c.fetchval(
            "SELECT status FROM sali.task_step WHERE task_id=$1 AND seq=1", task.id)
    assert p == "pending", "parent must stay pending until all children settle"

    # Second child - parent should NOW auto-close.
    await store.advance(task.id, 3, "skipped")
    async with live_pool.acquire() as c:
        p = await c.fetchval(
            "SELECT status FROM sali.task_step WHERE task_id=$1 AND seq=1", task.id)
    assert p == "done", "parent must auto-close when all children settle"


async def test_parent_cascade_handles_cycle_safely(live_pool: Any) -> None:
    """A pathological parent_seq cycle must NOT loop forever - visited set guards."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Cycle task", ["a", "b"])
    async with live_pool.acquire() as c:
        # Force a self-parent (pathological) - would loop without the visited guard.
        await c.execute(
            "UPDATE sali.task_step SET parent_seq = seq WHERE task_id = $1", task.id)
        # This should terminate immediately.
        await _parent_auto_close(c, task.id, 1, publisher=None)
    # If we got here without a hang, the guard worked.


# ── (5) failed step never cascades ──────────────────────────────────────────────

async def test_failed_child_does_not_close_parent(live_pool: Any) -> None:
    """A failed child must NOT trigger the parent cascade - that would silently close
    a parent whose child is actually broken."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Parent + failing child", ["parent"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_step (task_id, seq, description, parent_seq) "
            "VALUES ($1, 2, 'the only child', 1)", task.id)

    await store.advance(task.id, 2, "failed", error="something broke")
    async with live_pool.acquire() as c:
        p = await c.fetchval(
            "SELECT status FROM sali.task_step WHERE task_id=$1 AND seq=1", task.id)
    assert p == "pending", "a failed child must never close the parent"


# ── Turn 7 hardening regression: parent-cascade triggers task auto-complete ──

async def test_parent_cascade_triggers_task_auto_complete(live_pool: Any) -> None:
    """The whole point of the tally + cascade: a task with only a parent step + two
    children auto-completes when both children settle. The cascade closes the parent,
    the recomputed tally sees all-settled, and the task flips to done."""
    store = TaskStore(live_pool, EventPublisher(live_pool))
    task = await store.create("Parent + 2 children", ["parent step"])
    await store.activate(task.id)
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task_step (task_id, seq, description, parent_seq) "
            "VALUES ($1, 2, 'child A', 1), ($1, 3, 'child B', 1)", task.id)

    await store.advance(task.id, 2, "done")
    await store.advance(task.id, 3, "done")   # settles last child → cascade → parent done → task done

    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT status FROM sali.task WHERE id=$1", task.id)
    assert row["status"] == "done", (
        f"task must auto-complete after the parent cascade settles the parent step; got {row['status']}")
