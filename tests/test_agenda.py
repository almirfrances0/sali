"""The AgendaSynthesiser is a READ-ONLY cross-store view.

Prompt-8-followup brief §3: the agenda represents what matters to Sali now/next/later/eventually,
built from the live stores (task, commitment, schedule, goal, initiative). This test suite pins the
contract at the reader's edge - what lands in which section, what happens when a store is empty,
what happens across the owner's local-day boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sali.core.clock import FrozenClock
from sali.core.temporal import TemporalService
from sali.tasks.agenda import AgendaSynthesiser, render

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)   # 17:30 in Dar es Salaam (UTC+3)
DAR = "Africa/Dar_es_Salaam"


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any: return conn
            async def __aexit__(self, *exc: Any) -> bool: return False

        return _Ctx()


def _syn(conn: Any) -> AgendaSynthesiser:
    return AgendaSynthesiser(_Pool(conn), TemporalService(FrozenClock(NOW), owner_timezone=DAR))


async def test_an_empty_system_produces_an_empty_view(db_conn: Any) -> None:
    """The synthesiser must never invent structure. Nothing in any store = nothing in any section."""
    view = await _syn(db_conn).synthesise()
    assert view.is_empty()
    assert render(view) is None, "empty view renders to None so the caller omits the header"


async def test_an_active_primary_task_lands_in_NOW(db_conn: Any) -> None:
    """The 'now' section always leads with the primary active task if there is one - it is what
    Sali is on right this moment, and everything else is context around it."""
    # Set created_at explicitly so task_times_ordered (started_at >= created_at) holds.
    await db_conn.execute(
        "INSERT INTO task (objective, status, is_primary, created_at, started_at) "
        "VALUES ('archive last quarter docs','running', true, $1, $1)",
        NOW - timedelta(minutes=47))
    view = await _syn(db_conn).synthesise()
    assert len(view.now) == 1
    item = view.now[0]
    assert item.kind == "task" and "archive" in item.title
    assert "47" in item.when or "48" in item.when, item.when


async def test_a_commitment_due_this_hour_lands_in_NOW(db_conn: Any) -> None:
    """The other 'now' member: something Sali said he'd do that comes due imminently."""
    due = NOW + timedelta(minutes=20)
    await db_conn.execute(
        "INSERT INTO commitment (description, status, deadline) "
        "VALUES ('finish the archive script', 'open', $1)", due)
    view = await _syn(db_conn).synthesise()
    nows = [i for i in view.now if i.kind == "commitment"]
    assert len(nows) == 1
    assert "archive script" in nows[0].title


async def test_a_commitment_due_tomorrow_lands_in_TODAY_only_if_still_todays_civil_day(
        db_conn: Any) -> None:
    """Timezone matters. Almir at UTC+3: 'today' ends at 21:00 UTC. A commitment due at 22:00 UTC
    (which is 01:00 Dar next day) must NOT land in today. This is the whole point of the
    civil-day boundary computation."""
    tomorrow_local = NOW + timedelta(hours=8)   # 22:30 UTC = 01:30 Dar (tomorrow)
    await db_conn.execute(
        "INSERT INTO commitment (description, status, deadline) VALUES ('x','open',$1)",
        tomorrow_local)
    view = await _syn(db_conn).synthesise()
    assert view.today == [], "22:30 UTC is Almir's tomorrow, must not be in today"


async def test_an_overdue_commitment_surfaces_with_how_late(db_conn: Any) -> None:
    """Overdue is where accountability lives. The 'when' text says how long past the deadline it is,
    computed by the SAME temporal service Sali uses to talk about time elsewhere."""
    await db_conn.execute(
        "INSERT INTO commitment (description, status, deadline) VALUES ('x','open',$1)",
        NOW - timedelta(hours=2))
    view = await _syn(db_conn).synthesise()
    assert len(view.overdue) == 1
    assert "ago" in view.overdue[0].when


async def test_a_blocked_task_and_a_waiting_task_land_in_the_right_sections(db_conn: Any) -> None:
    """Two distinct states, two distinct reasons Almir needs to know. 'blocked' means external
    dependency; 'waiting' means Sali paused on a question to Almir."""
    await db_conn.execute("INSERT INTO task (objective, status) VALUES ('a','blocked')")
    await db_conn.execute("INSERT INTO task (objective, status) VALUES ('b','waiting')")
    view = await _syn(db_conn).synthesise()
    assert len(view.blocked) == 1 and view.blocked[0].title == "a"
    assert len(view.waiting) == 1 and view.waiting[0].title == "b"


async def test_upcoming_schedules_are_after_today(db_conn: Any) -> None:
    """The 'upcoming' section is the medium-horizon view: what fires AFTER today. A schedule due
    today belongs in 'today', not upcoming - and the test enforces that separation."""
    next_run_today = NOW + timedelta(hours=1)   # 15:30 UTC = 18:30 Dar (still today)
    next_run_tomorrow = NOW + timedelta(days=1)  # tomorrow same time
    await db_conn.execute(
        "INSERT INTO schedule (name, kind, spec, prompt, next_run_at, enabled, timezone) "
        "VALUES ('today','interval','1h','ping', $1, true, 'UTC')", next_run_today)
    await db_conn.execute(
        "INSERT INTO schedule (name, kind, spec, prompt, next_run_at, enabled, timezone) "
        "VALUES ('tomorrow','interval','1d','ping', $1, true, 'UTC')", next_run_tomorrow)
    view = await _syn(db_conn).synthesise()
    today_names = [i.title for i in view.today]
    upcoming_names = [i.title for i in view.upcoming]
    assert "today" in today_names and "today" not in upcoming_names
    assert "tomorrow" in upcoming_names and "tomorrow" not in today_names


async def test_render_produces_only_the_sections_with_content(db_conn: Any) -> None:
    """render() must not print an 'Overdue:' header when there are no overdue items - a hollow
    label is worse than silence. This test also acts as the contract for the prompt injection: what
    the model sees is exactly what a person would want to see."""
    await db_conn.execute("INSERT INTO task (objective, status) VALUES ('the one thing','blocked')")
    view = await _syn(db_conn).synthesise()
    out = render(view)
    assert out is not None
    assert "Blocked:" in out and "the one thing" in out
    assert "Overdue:" not in out and "Today:" not in out, "empty sections must be omitted"


async def test_the_agenda_is_JSON_serialisable_for_the_iPhone(db_conn: Any) -> None:
    """The iOS app reads via /api/v1/agenda; the shape must serialise cleanly. If any field ends up
    non-serialisable (raw datetime, UUID object) the app can't decode - fail fast in a test rather
    than in production."""
    import json
    await db_conn.execute("INSERT INTO task (objective, status) VALUES ('x','blocked')")
    view = await _syn(db_conn).synthesise()
    payload = view.to_json()
    encoded = json.dumps(payload)
    assert isinstance(encoded, str) and len(encoded) > 0
