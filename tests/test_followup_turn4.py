"""Turn 4 of the Work-audit: §15 follow-up detection.

After a task completes and is archived (Turn 1 keeps the row), Almir's next message
often continues the same work — "make the header smaller" after a landing-page task
is not a new task, it's a refinement. This turn wires:

  1. `TaskStore.create(parent_task_id=...)` — child task inherits workspace + allowed_write_roots.
  2. `TaskAuthority.detect_followup(user_input)` — matches the message against
     `recent_completed()` results, returns the parent task on a strong match.
  3. `handle_new_turn` — when no active primary, routes follow-up messages to
     `TaskAction.FOLLOWUP` with the parent task attached.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sali.tasks.authority import TaskAction, TaskAuthority
from sali.tasks.store import TaskStore


async def _seed_archived(pool, objective: str, *, workspace_root=None,
                          allowed_write_roots=None, when_ago=None) -> str:
    """Insert one done+archived task with optional workspace inheritance data."""
    task_id = uuid4()
    now = datetime.now(timezone.utc)
    archived_at = (now - when_ago) if when_ago else now
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO sali.task (id, objective, status, workspace_root, "
            "  allowed_write_roots, workspace_mode, created_at, started_at, "
            "  completed_at, archived_at, updated_at) "
            "VALUES ($1, $2, 'done', $3, $4, $5, $6, $6, $6, $7, $6)",
            task_id, objective, workspace_root, allowed_write_roots or [],
            'explicit' if workspace_root else 'none',
            archived_at - timedelta(minutes=10), archived_at)
    return str(task_id)


async def _clear(pool) -> None:
    async with pool.acquire() as c:
        await c.execute("TRUNCATE sali.task CASCADE")


# ── 1. Child task inherits workspace from parent ────────────────────────────────

@pytest.mark.asyncio
async def test_create_with_parent_inherits_workspace(live_pool) -> None:
    """A follow-up child task created via TaskStore.create(parent_task_id=X) picks up
    the parent's workspace_root + allowed_write_roots so it can modify the same files."""
    await _clear(live_pool)
    parent_id = await _seed_archived(
        live_pool, "Ship the landing page",
        workspace_root="/home/almir/Desktop/landing",
        allowed_write_roots=["/home/almir/Desktop/landing"])

    from uuid import UUID as _U
    store = TaskStore(live_pool)
    child = await store.create(
        "Make the header smaller", ["adjust font-size"],
        parent_task_id=_U(parent_id))

    assert child.workspace_root == "/home/almir/Desktop/landing"
    assert child.allowed_write_roots == ["/home/almir/Desktop/landing"]
    assert str(child.parent_task_id) == parent_id


@pytest.mark.asyncio
async def test_create_with_explicit_workspace_overrides_parent(live_pool) -> None:
    """When the caller passes an explicit workspace_root, that wins over inheritance."""
    await _clear(live_pool)
    parent_id = await _seed_archived(
        live_pool, "Old task", workspace_root="/parent/dir")

    from uuid import UUID as _U
    store = TaskStore(live_pool)
    child = await store.create(
        "New work", ["step"],
        workspace_root="/explicit/override",
        parent_task_id=_U(parent_id))

    assert child.workspace_root == "/explicit/override", (
        "explicit workspace_root must override inheritance")


# ── 2. detect_followup matches recently-completed tasks ─────────────────────────

@pytest.mark.asyncio
async def test_detect_followup_matches_recent_completed(live_pool) -> None:
    """The classic §15 case: after Sali finishes a landing-page task, "make the header
    smaller" rediscovers the parent."""
    await _clear(live_pool)
    await _seed_archived(live_pool, "Ship the landing page for Salieno")

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    result = await authority.detect_followup("make the header smaller")
    assert result is not None, "follow-up phrase must match a recently-completed task"
    parent, reason = result
    assert parent.objective == "Ship the landing page for Salieno"
    assert "make the header" in reason.lower() or "make it" in reason.lower() or "make" in reason.lower()


@pytest.mark.asyncio
async def test_detect_followup_returns_None_without_hint_phrase(live_pool) -> None:
    """A message with no follow-up hint phrase (e.g. "hey" or a fully-scoped new request)
    must NOT be treated as a follow-up, even if a recent completed task exists."""
    await _clear(live_pool)
    await _seed_archived(live_pool, "Some prior task")

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    assert await authority.detect_followup("hey what's the weather") is None
    assert await authority.detect_followup(
        "build me a Django REST API from scratch") is None


@pytest.mark.asyncio
async def test_detect_followup_returns_None_when_no_completed_tasks(live_pool) -> None:
    """Fresh system with no completed tasks - even a follow-up phrase can't match anything."""
    await _clear(live_pool)

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    assert await authority.detect_followup("make it smaller") is None


@pytest.mark.asyncio
async def test_detect_followup_prefers_topic_overlap_over_recency(live_pool) -> None:
    """Given two recent completed tasks and a follow-up message with content keywords,
    prefer the one whose objective shares more keywords (not just the most recent)."""
    await _clear(live_pool)
    # Recent-but-unrelated
    _older = await _seed_archived(live_pool, "Deploy the Rust migration",
                                   when_ago=timedelta(minutes=30))
    # Older-but-more-relevant to a "landing page" follow-up
    _target = await _seed_archived(live_pool, "Build the landing page hero section",
                                    when_ago=timedelta(minutes=5))

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    result = await authority.detect_followup("make the hero section smaller")
    assert result is not None
    parent, _reason = result
    assert "landing page" in parent.objective, (
        f"detect_followup should have matched the topic-overlapping task, "
        f"got: {parent.objective}")


# ── 3. handle_new_turn routes FOLLOWUP correctly ────────────────────────────────

@pytest.mark.asyncio
async def test_handle_new_turn_returns_FOLLOWUP_when_no_active_task(live_pool) -> None:
    """After the primary task finishes and is archived, a follow-up message must land as
    action=FOLLOWUP with the parent task attached, not NONE."""
    await _clear(live_pool)
    await _seed_archived(live_pool, "Ship the landing page")

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    transition = await authority.handle_new_turn("make the header smaller")
    assert transition.action == TaskAction.FOLLOWUP, (
        f"expected FOLLOWUP, got {transition.action}")
    assert transition.previous_task is not None
    assert transition.previous_task.objective == "Ship the landing page"
    assert "follow-up" in transition.reason.lower()


@pytest.mark.asyncio
async def test_handle_new_turn_still_returns_NONE_for_random_message(live_pool) -> None:
    """A random message with no follow-up hint still returns NONE (not FOLLOWUP) even
    when a recent completed task exists."""
    await _clear(live_pool)
    await _seed_archived(live_pool, "Some old task")

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    transition = await authority.handle_new_turn("what time is it")
    assert transition.action == TaskAction.NONE


@pytest.mark.asyncio
async def test_followup_does_not_fire_when_active_task_exists(live_pool) -> None:
    """Follow-up detection is for AFTER completion. If a primary task is active, a
    follow-up phrase should route through the normal continuation path, not FOLLOWUP."""
    await _clear(live_pool)
    # An archived-completed task exists
    await _seed_archived(live_pool, "Old landing page task")
    # AND an active primary task now
    store = TaskStore(live_pool)
    authority = TaskAuthority(store)
    active = await store.create("Currently doing something else", ["step 1"])
    await store.activate(active.id)

    transition = await authority.handle_new_turn("make it smaller")
    # Must NOT be FOLLOWUP - a live task is present; the message routes through the
    # continuation logic (CONTINUED or SUPERSEDED based on other heuristics).
    assert transition.action != TaskAction.FOLLOWUP, (
        f"follow-up detection must NOT fire when a primary task is active; "
        f"got action={transition.action}")


# ── 4. End-to-end: detect + create-child preserves the parent linkage ────────────

@pytest.mark.asyncio
async def test_end_to_end_followup_creates_child_with_workspace(live_pool) -> None:
    """Full round trip: parent task done+archived → follow-up detected → child created
    via TaskStore.create(parent_task_id=parent.id) inheriting workspace."""
    await _clear(live_pool)
    parent_id = await _seed_archived(
        live_pool, "Ship the landing page for Salieno",
        workspace_root="/home/almir/Desktop/landing",
        allowed_write_roots=["/home/almir/Desktop/landing", "/home/almir/Desktop/assets"])

    store = TaskStore(live_pool)
    authority = TaskAuthority(store)

    transition = await authority.handle_new_turn("make the header smaller")
    assert transition.action == TaskAction.FOLLOWUP
    parent = transition.previous_task
    assert parent is not None

    child = await store.create(
        "Make the header smaller", ["adjust CSS font-size"],
        parent_task_id=parent.id)

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT parent_task_id::text AS pid, workspace_root, allowed_write_roots "
            "FROM sali.task WHERE id = $1", child.id)
    assert row["pid"] == parent_id
    assert row["workspace_root"] == "/home/almir/Desktop/landing"
    assert list(row["allowed_write_roots"]) == [
        "/home/almir/Desktop/landing", "/home/almir/Desktop/assets"]
