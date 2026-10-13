"""The scheduler (§44): the self-contained cron/interval engine, the store, the daemon, the tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import FrozenClock, SystemClock
from sali.scheduler import cron
from sali.scheduler.cron import ScheduleError
from sali.scheduler.daemon import SchedulerDaemon
from sali.scheduler.store import ScheduleStore
from sali.tools.builtins.schedule_tool import CancelSchedule, ScheduleTask
from sali.tools.context import ToolContext


# ---- the cron/interval engine (pure, no DB) ----------------------------------------------------
def test_parse_when_detects_kind_and_rejects_garbage() -> None:
    assert cron.parse_when("30m") == ("interval", "30m")
    assert cron.parse_when(" 2h ") == ("interval", "2h")
    assert cron.parse_when("0 9 * * *") == ("cron", "0 9 * * *")
    for bad in ("garbage", "0 9 * *", "0 99 * * *", "0 9 * * 9", "every day"):
        with pytest.raises(ScheduleError):
            cron.parse_when(bad)


def test_interval_next_run() -> None:
    t = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert cron.next_run("interval", "30m", t) == t + timedelta(minutes=30)
    assert cron.next_run("interval", "2h", t) == t + timedelta(hours=2)
    assert cron.next_run("interval", "1d", t) == t + timedelta(days=1)
    assert cron.next_run("interval", "45s", t) == t + timedelta(seconds=45)


def test_cron_daily_and_step_and_weekly() -> None:
    # daily at 09:00
    before = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
    assert cron.next_run("cron", "0 9 * * *", before) == datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    after = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    assert cron.next_run("cron", "0 9 * * *", after) == datetime(2026, 8, 27, 9, 0, tzinfo=UTC)

    # every 15 minutes
    t = datetime(2026, 8, 26, 9, 7, tzinfo=UTC)
    assert cron.next_run("cron", "*/15 * * * *", t) == datetime(2026, 8, 26, 9, 15, tzinfo=UTC)

    # weekly on Sunday at midnight (cron dow 0 == Sunday)
    nxt = cron.next_run("cron", "0 0 * * 0", datetime(2026, 8, 26, 12, 0, tzinfo=UTC))
    assert nxt.isoweekday() == 7 and nxt.hour == 0 and nxt.minute == 0 and nxt > t


def test_cron_range_list_and_dom_dow_or_rule() -> None:
    # ranges + lists: 08:00 and 08:30 on weekdays (Mon-Fri)
    t = datetime(2026, 8, 26, 8, 15, tzinfo=UTC)  # a Wednesday
    assert cron.next_run("cron", "0,30 8 * * 1-5", t) == datetime(2026, 8, 26, 8, 30, tzinfo=UTC)

    # dom AND dow both restricted → cron's OR rule: fires on the 13th OR any Friday, whichever first.
    nxt = cron.next_run("cron", "0 12 13 * 5", datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
    assert nxt.hour == 12 and (nxt.day == 13 or nxt.isoweekday() == 5)


def test_cron_multi_year_scan_for_leap_day() -> None:
    # Feb 29 exists only on a leap year → the day-scan must look years ahead, not give up.
    nxt = cron.next_run("cron", "0 0 29 2 *", datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    assert nxt.month == 2 and nxt.day == 29 and nxt.year >= 2028


# ---- the store (DB) ----------------------------------------------------------------------------
@pytest.mark.db
async def test_store_create_due_and_advance(live_pool: Any) -> None:
    clock = FrozenClock(datetime(2026, 8, 26, 12, 0, tzinfo=UTC))
    store = ScheduleStore(live_pool, clock)

    sched = await store.create("morning-check", "30m", "check the disk and tell me")
    assert sched.kind == "interval" and sched.next_run_at == clock.now() + timedelta(minutes=30)
    assert not await store.due()  # not due yet — it's 30m out

    clock.advance(31 * 60)  # jump past the fire time
    due = await store.due()
    assert len(due) == 1 and due[0].name == "morning-check"

    await store.mark_fired(sched.id, "ok")
    assert not await store.due()  # advanced to the next slot (30m from the new now)
    rows = await store.list_all()
    assert rows[0].last_status == "ok"


@pytest.mark.db
async def test_store_set_enabled_and_delete(live_pool: Any) -> None:
    clock = FrozenClock(datetime(2026, 8, 26, 12, 0, tzinfo=UTC))
    store = ScheduleStore(live_pool, clock)
    await store.create("nightly", "0 3 * * *", "back up")
    clock.advance(24 * 3600)

    await store.set_enabled("nightly", False)
    assert not await store.due()  # paused → never due even when its time passes

    assert await store.delete("nightly") == 1
    assert await store.delete("nightly") == 0  # already gone


# ---- the daemon (DB + a fake runner) -----------------------------------------------------------
class _FakeRunner:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, prompt: str) -> str:
        self.ran.append(prompt)
        return "done"


@pytest.mark.db
async def test_daemon_tick_fires_due_schedules(live_pool: Any) -> None:
    clock = FrozenClock(datetime(2026, 8, 26, 12, 0, tzinfo=UTC))
    store = ScheduleStore(live_pool, clock)
    await store.create("job", "15m", "do the thing")
    clock.advance(16 * 60)  # now due

    runner = _FakeRunner()
    daemon = SchedulerDaemon(store, runner, pool=live_pool, poll_s=1.0)
    fired = await daemon.tick()

    assert fired == 1 and runner.ran == ["do the thing"]
    assert not await store.due()  # marked fired + advanced
    async with live_pool.acquire() as c:
        events = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='schedule.fired'")
    assert events >= 1  # the fire was recorded to the audit log


# ---- the tools (fake sink) ---------------------------------------------------------------------
class _FakeSchedules:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []

    async def create(self, name: str, when: str, prompt: str) -> Any:
        _kind, _spec = cron.parse_when(when)  # raise ScheduleError on a bad spec, like the real store
        self.created.append((name, when, prompt))
        return type("S", (), {"next_run_at": datetime(2026, 8, 27, 9, 0, tzinfo=UTC)})()

    async def delete(self, name: str) -> int:
        self.deleted.append(name)
        return 1 if name == "known" else 0


def _ctx(schedules: _FakeSchedules | None) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), schedules=schedules)


async def test_schedule_tool_creates_and_rejects_bad_spec() -> None:
    sinks = _FakeSchedules()
    ok = await ScheduleTask().run(
        {"name": "am", "when": "0 9 * * *", "prompt": "check disk"}, _ctx(sinks))
    assert ok.ok and sinks.created == [("am", "0 9 * * *", "check disk")]

    bad = await ScheduleTask().run(
        {"name": "x", "when": "not a schedule", "prompt": "y"}, _ctx(_FakeSchedules()))
    assert not bad.ok and "schedule" in (bad.error or "").lower()


async def test_cancel_schedule_tool() -> None:
    sinks = _FakeSchedules()
    hit = await CancelSchedule().run({"name": "known"}, _ctx(sinks))
    assert hit.ok and sinks.deleted == ["known"]
    miss = await CancelSchedule().run({"name": "nope"}, _ctx(sinks))
    assert not miss.ok and "no schedule" in (miss.error or "")
