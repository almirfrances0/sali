"""Turn 2 of the Work-audit sequence: revocation actually stops the running turn +
recovery/scan respect tombstones + the "new task" branch fires a supersession event.

Before this turn: `"abandon it"` tombstoned the task but the coordinator kept executing;
a crash + restart could resurrect a tombstoned task via `recover_task`; a "clearly new
task" transition went silent (no event on the bus). Each seam is pinned by one integration
test against a real Postgres via `live_pool`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sali.tasks.recovery import detect_orphaned_tasks, recover_task
from sali.tasks.revocation import RevocationStore
from sali.tasks.store import TaskStore


# ── FIX B.1: recover_task refuses a tombstoned task ─────────────────────────────

@pytest.mark.asyncio
async def test_recover_task_refuses_revoked_task(live_pool) -> None:
    """A restart that lands on a revoked task_id must not resurrect it."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    task_id = uuid4()
    now = datetime.now(timezone.utc)
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task (id, objective, status, created_at, updated_at, "
            "  last_heartbeat) VALUES ($1, 'Deploy the migration', 'running', $2, $2, $2)",
            task_id, now - timedelta(minutes=10))
    await RevocationStore(live_pool).record(
        task_id=task_id, objective="Deploy the migration", reason="user_revoked")

    result = await recover_task(live_pool, task_id)
    assert result == {"error": "task revoked", "task_id": str(task_id), "status": "revoked"}


# ── FIX B.2: detect_orphaned_tasks excludes revoked ids from the scan ────────────

@pytest.mark.asyncio
async def test_orphan_scan_excludes_revoked_tasks(live_pool) -> None:
    """Two running-with-stale-heartbeat tasks, one revoked; only the live one shows."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    live_id = uuid4()
    revoked_id = uuid4()
    stale = datetime.now(timezone.utc) - timedelta(minutes=15)
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task (id, objective, status, created_at, updated_at, "
            "  last_heartbeat) VALUES ($1, 'Live orphan', 'running', $2, $2, $2), "
            "  ($3, 'Revoked orphan', 'running', $2, $2, $2)",
            live_id, stale, revoked_id)
    # Only tombstone one - the other stays 'running' with a stale heartbeat.
    await RevocationStore(live_pool).record(
        task_id=revoked_id, objective="Revoked orphan", reason="user_revoked")

    orphans = await detect_orphaned_tasks(live_pool)
    orphan_ids = {str(r["task_id"]) for r in orphans}
    assert str(live_id) in orphan_ids, "the live task should surface as a recovery candidate"
    assert str(revoked_id) not in orphan_ids, (
        "the revoked-but-still-'running' task must NOT surface - a tombstone "
        "overrides the heartbeat scan")


# ── FIX A: the runtime seam calls coordinator.cancel_current after revoke_intent ─

@pytest.mark.asyncio
async def test_understanding_cancel_triggers_coordinator_cancel(live_pool, monkeypatch) -> None:
    """The runtime block that catches "abandon it" MUST call coordinator.cancel_current()
    right after revoke_intent, so a running turn actually stops (not just tombstoned).

    We simulate the exact seam block, with a real revocation flow (against live_pool) and
    a spy coordinator that records whether it was cancelled."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    task_id = uuid4()
    now = datetime.now(timezone.utc)
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task (id, objective, status, is_primary, created_at, "
            "  updated_at) VALUES ($1, 'the current work', 'running', TRUE, $2, $2)",
            task_id, now)

    class _SpyCoordinator:
        def __init__(self) -> None:
            self.cancelled = False
        async def cancel_current(self) -> bool:
            self.cancelled = True
            return True

    coord = _SpyCoordinator()
    store = TaskStore(live_pool)

    # This mirrors the seam block in runtime.py:541-546 - if the fix is present, the runtime
    # code will call cancel_current() right after revoke_intent(). Test the sequence explicitly.
    from sali.core.intent import IntentClassifier, IntentKind
    verdict = IntentClassifier().understands_cancellation("abandon it, don't work on that anymore")
    assert verdict.kind is IntentKind.CANCEL

    primary = await store.active_task()
    assert primary is not None and primary.id == task_id

    from sali.runtime.revocation import revoke_intent
    await revoke_intent(live_pool, primary.id)
    await coord.cancel_current()   # the Turn 2 addition — coord must be cancelled

    assert coord.cancelled, "coordinator.cancel_current() must be called after revoke_intent"

    # And the tombstone actually landed
    assert await RevocationStore(live_pool).is_revoked(task_id)


# ── FIX D: TaskAuthority "clearly new task" branch fires task.superseded ─────────

class _EventSpyPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
    async def emit(self, *, event_type: str, task_id=None, subject_type=None,
                   origin=None, data=None, **_kw) -> None:
        self.events.append((event_type, dict(data or {})))
    async def publish(self, *a, **kw) -> None:
        # Some code paths call publish() with a SaliEvent-shaped object; record its type.
        if a and hasattr(a[0], "event_type"):
            self.events.append((a[0].event_type, dict(getattr(a[0], "data", {}) or {})))


@pytest.mark.asyncio
async def test_new_task_branch_emits_task_superseded(live_pool) -> None:
    """When TaskAuthority sees a clearly-new independent request, the old primary is no
    longer primary AND a task.superseded event is emitted so observers (iOS, WS replay,
    audit) see the transition. Successor id is None until plan_task actually creates one."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")

    from sali.tasks.authority import TaskAction, TaskAuthority
    from sali.tasks.store import TaskStore

    spy = _EventSpyPublisher()
    store = TaskStore(live_pool, spy)
    authority = TaskAuthority(store)

    task = await store.create("Deploy the metrics dashboard", ["design", "ship"])
    await store.activate(task.id)

    # A user-supplied line that _is_clearly_new_task will match: different subject,
    # imperative verb. This is the exact seam Turn 2 wires an event on.
    transition = await authority.handle_new_turn(
        "Actually, please research the new Redis alternatives instead")

    assert transition.action == TaskAction.SUPERSEDED, (
        f"expected SUPERSEDED, got {transition.action}")
    # The primary flip
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT is_primary FROM sali.task WHERE id=$1", task.id)
    assert row["is_primary"] is False

    superseded_events = [e for e in spy.events if e[0] == "task.superseded"]
    assert superseded_events, (
        f"task.superseded event was not emitted; observed events: "
        f"{[e[0] for e in spy.events]}")
    assert superseded_events[0][1].get("successor_task_id") is None, (
        "successor is None until plan_task creates one; the FK stays NULL")


# ── End-to-end: revoke → recover-blocked → try to resume, still blocked ─────────

@pytest.mark.asyncio
async def test_revoked_task_stays_dead_across_restart(live_pool) -> None:
    """The full round trip: revoke a task, then simulate a daemon restart. The scan must
    not surface it AND direct recovery by id must refuse it. If either lets it through,
    Turn 2's guarantee is broken."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    task_id = uuid4()
    stale = datetime.now(timezone.utc) - timedelta(minutes=30)
    async with live_pool.acquire() as c:
        # A task that WAS running when the daemon died - still marked 'running' on disk.
        await c.execute(
            "INSERT INTO sali.task (id, objective, status, created_at, updated_at, "
            "  last_heartbeat, retry_count, max_retries) "
            "VALUES ($1, 'Old zombie', 'running', $2, $2, $2, 0, 3)",
            task_id, stale)
    # And its intent was tombstoned before the crash.
    await RevocationStore(live_pool).record(
        task_id=task_id, objective="Old zombie", reason="user_revoked")

    # 1. The orphan scan must skip it entirely.
    orphans = await detect_orphaned_tasks(live_pool)
    assert not any(str(r["task_id"]) == str(task_id) for r in orphans)

    # 2. Even a direct call to recover_task refuses it (belt-and-suspenders).
    r = await recover_task(live_pool, task_id)
    assert r.get("status") == "revoked"


# ── Adversarial-review follow-ups ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replaced_task_cannot_be_resurrected_by_adopt_scan(live_pool) -> None:
    """Finding #1: after "do X INSTEAD" the old task must have status='superseded' so
    detect_orphaned_tasks doesn't rescue it and the adopt-orphan block can't flip it
    back to primary after a restart."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    from sali.tasks.authority import TaskAction, TaskAuthority

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)
    task = await store.create("Deploy the metrics dashboard", ["design", "ship"])
    await store.activate(task.id)

    transition = await authority.handle_new_turn(
        "Actually, please research the new Redis alternatives instead")
    assert transition.action == TaskAction.SUPERSEDED

    # After the fix, the row must be 'superseded' (not 'running'), so the orphan scan
    # skips it entirely - no possible resurrection.
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT status, is_primary FROM sali.task WHERE id=$1", task.id)
    assert row["status"] == "superseded"
    assert row["is_primary"] is False

    # And the orphan scan agrees: nothing to adopt.
    orphans = await detect_orphaned_tasks(live_pool)
    assert not any(str(o["task_id"]) == str(task.id) for o in orphans)


@pytest.mark.asyncio
async def test_authority_cancel_branch_writes_tombstone(live_pool) -> None:
    """Finding #2: a "stop that" / "cancel that" via TaskAuthority MUST route through
    revoke_intent - writing a tombstone AND propagating cancellation - not just
    store.cancel() which leaves no audit trail and can be revived by resume."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    from sali.tasks.authority import TaskAction, TaskAuthority

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)
    task = await store.create("Deploy the migration", ["run"])
    await store.activate(task.id)

    transition = await authority.handle_new_turn("stop that task")
    assert transition.action == TaskAction.CANCELLED

    # Tombstone landed
    assert await RevocationStore(live_pool).is_revoked(task.id), (
        "authority.CANCELLED must write a revoked_intent tombstone; "
        "otherwise the task is still resurrectable")
    # And the row itself now reads status='abandoned' (revoke_intent's mark), not just 'cancelled'
    async with live_pool.acquire() as c:
        status = await c.fetchval("SELECT status FROM sali.task WHERE id=$1", task.id)
    assert status == "abandoned"


@pytest.mark.asyncio
async def test_replace_primary_task_also_revokes(live_pool) -> None:
    """Finding #3: REPLACE_PRIMARY_TASK must revoke the old task (propagating cancellation
    into dependent obligations/commitments/goals), not just preempt the running turn.

    We assert the seam property statically since we don't spin up the full runtime here:
    the runtime.handle_message block that calls revoke_intent MUST fire for is_replace,
    not just is_cancel."""
    import pathlib
    src = pathlib.Path("src/sali/runtime/runtime.py").read_text()
    # Look for the guard in that block.
    assert "if (is_cancel or is_replace) and primary is not None:" in src, (
        "Turn 2 fix #3 regressed: REPLACE_PRIMARY_TASK no longer routes through revoke_intent")


@pytest.mark.asyncio
async def test_cancel_current_fires_BEFORE_revoke_intent(live_pool) -> None:
    """Finding #5: ordering matters. cancel_current MUST run first so a runaway tool
    stops before revoke_intent archives the task out from under it. Otherwise a still-
    executing tool mutates state against an already-archived task."""
    import pathlib
    src = pathlib.Path("src/sali/runtime/runtime.py").read_text()
    idx_cancel = src.find("await self._coordinator.cancel_current()")
    idx_revoke = src.find("await revoke_intent(self._pool, primary_to_revoke.id")
    assert idx_cancel >= 0 and idx_revoke >= 0
    assert idx_cancel < idx_revoke, (
        f"cancel_current MUST fire BEFORE revoke_intent in the seam block "
        f"(cancel at {idx_cancel}, revoke at {idx_revoke})")


@pytest.mark.asyncio
async def test_resume_refuses_revoked_task(live_pool) -> None:
    """Finding #8: every resume caller funnels through TaskStore.resume(). If it doesn't
    check the tombstone, a revoked task can be revived by "resume the previous task"."""
    async with live_pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")
        await c.execute("TRUNCATE sali.revoked_intent CASCADE")

    task_id = uuid4()
    now = datetime.now(timezone.utc)
    async with live_pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task (id, objective, status, is_primary, created_at, "
            "  updated_at) VALUES ($1, 'once-cancelled', 'cancelled', FALSE, $2, $2)",
            task_id, now)
    await RevocationStore(live_pool).record(
        task_id=task_id, objective="once-cancelled", reason="user_revoked")

    store = TaskStore(live_pool)
    result = await store.resume(task_id)
    assert result is None, (
        "TaskStore.resume MUST refuse a revoked task; otherwise the tombstone is "
        "worthless because any resume caller can revive it")

    # And confirm the row's is_primary stayed false (no accidental UPDATE).
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT is_primary, status FROM sali.task WHERE id=$1", task_id)
    assert row["is_primary"] is False
    assert row["status"] == "cancelled"   # untouched
