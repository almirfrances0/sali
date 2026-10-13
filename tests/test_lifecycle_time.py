"""The four temporal gaps that were still open: schedules, task duration, promises, downtime.

Each is a question Almir can ask that had no answer in the data: when does "every day at 9am" actually
fire, how long has this task been running, is that promise overdue, and how long was Sali away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sali.scheduler.cron import next_run

DAR = "Africa/Dar_es_Salaam"
NY = "America/New_York"


# ── §12 a cron spec is civil time ───────────────────────────────────────────────────────────────────

def test_nine_am_means_nine_am_where_almir_is() -> None:
    """Evaluated against UTC it fired at noon his time — the host is not the owner."""
    after = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
    fires = next_run("cron", "0 9 * * *", after, DAR)
    assert fires.astimezone(ZoneInfo(DAR)).strftime("%H:%M") == "09:00"
    assert fires == datetime(2026, 9, 4, 6, 0, tzinfo=UTC), "09:00 EAT is 06:00 UTC"


def test_a_daily_schedule_does_not_drift_across_dst() -> None:
    """The reason the ZONE is stored rather than a converted UTC hour: the offset must be re-derived at
    every firing, or a 09:00 alarm silently becomes 08:00 the day the clocks change."""
    before = next_run("cron", "0 9 * * *", datetime(2026, 10, 31, 12, tzinfo=UTC), NY)
    after = next_run("cron", "0 9 * * *", datetime(2026, 11, 2, 12, tzinfo=UTC), NY)
    assert before.astimezone(ZoneInfo(NY)).strftime("%H:%M") == "09:00"
    assert after.astimezone(ZoneInfo(NY)).strftime("%H:%M") == "09:00"
    assert before.hour != after.hour, "the UTC instant must move; the civil time must not"


def test_an_interval_is_a_duration_and_ignores_the_zone() -> None:
    """"every 30m" means every thirty minutes wherever you are."""
    after = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
    assert next_run("interval", "30m", after, DAR) == next_run("interval", "30m", after)


# ── §13 how long has this been running ──────────────────────────────────────────────────────────────

@pytest.mark.db
async def test_a_task_records_when_work_began_and_ended(db_conn: Any) -> None:
    """created_at is when the task was WRITTEN DOWN. A task can sit queued behind another for an hour,
    so created→completed counts the waiting as working."""
    from sali.tasks.store import TaskStore

    class _Pool:
        def acquire(self) -> Any:
            conn = db_conn

            class _Ctx:
                async def __aenter__(self) -> Any:
                    return conn

                async def __aexit__(self, *exc: Any) -> bool:
                    return False

            return _Ctx()

    store = TaskStore(_Pool())
    # Three steps, and only two are advanced: a bare store auto-completes and ARCHIVES the task
    # the moment every step is done, which deletes the row this test is inspecting.
    task = await store.create("archive the photos", ["gather", "zip", "verify"])
    row = await db_conn.fetchrow("SELECT started_at, completed_at FROM task WHERE id=$1", task.id)
    assert row["started_at"] is None, "nothing has happened yet — inventing a start time would lie"
    assert row["completed_at"] is None

    await db_conn.execute("UPDATE task SET status='running' WHERE id=$1", task.id)
    await store.advance(task.id, 1, "done")
    started = await db_conn.fetchval("SELECT started_at FROM task WHERE id=$1", task.id)
    assert started is not None, "the first completed step is when work demonstrably began"

    await store.advance(task.id, 2, "done")
    again = await db_conn.fetchval("SELECT started_at FROM task WHERE id=$1", task.id)
    assert again == started, "a later step must not reset the clock"


@pytest.mark.db
async def test_the_database_refuses_an_impossible_timeline(db_conn: Any) -> None:
    """§48: a task cannot finish before it was created. The constraint is the enforcement."""
    import asyncpg

    task_id = await db_conn.fetchval(
        "INSERT INTO task (objective, status) VALUES ('x','open') RETURNING id")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db_conn.execute(
            "UPDATE task SET completed_at = created_at - interval '1 hour' WHERE id=$1", task_id)


# ── §14 a promise with a deadline ───────────────────────────────────────────────────────────────────

@pytest.mark.db
async def test_a_promise_with_a_deadline_becomes_a_commitment(db_conn: Any) -> None:
    """`CommitmentStore.create` had no caller anywhere: Sali's word was never written down, so nothing
    could later notice it had come due."""
    from sali.tasks.commitments import CommitmentStore

    class _Pool:
        def acquire(self) -> Any:
            conn = db_conn

            class _Ctx:
                async def __aenter__(self) -> Any:
                    return conn

                async def __aexit__(self, *exc: Any) -> bool:
                    return False

            return _Ctx()

    store = CommitmentStore(_Pool())
    deadline = datetime.now(UTC) + timedelta(hours=2)
    await store.create(description="I'll have the archive done by tonight", deadline=deadline)
    rows = await store.open(limit=5)
    assert len(rows) == 1
    assert rows[0]["deadline"] == deadline


def test_only_a_promise_with_a_named_TIME_is_a_commitment() -> None:
    """The trigger has to be narrow. "Let me check that" is an intention for this turn, not a
    commitment; recording every "I'll…" would fill the overdue list with noise until it meant nothing."""
    from sali.core.clock import FrozenClock
    from sali.core.temporal import TemporalService
    from sali.runtime.loop import _PROMISE_RE

    now = datetime(2026, 9, 3, 17, 43, tzinfo=UTC)
    clock = TemporalService(FrozenClock(now), owner_timezone=DAR)

    def would_capture(reply: str) -> bool:
        if not _PROMISE_RE.search(reply):
            return False
        window = clock.resolve(reply)
        return window is not None and window.end > now

    assert would_capture("I'll have it done by tomorrow")
    assert would_capture("I'll get back to you in 2 hours")
    assert not would_capture("Let me check that for you")
    assert not would_capture("I'll look into it")
    assert not would_capture("I'll finish what I started yesterday"), "a past time is not a deadline"
