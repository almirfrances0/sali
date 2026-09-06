"""Time as a dimension Sali actually reasons in.

The failure this replaces is on the record: on 2026-09-03 Sali told Almir they had been working
together "since last year (August 2024)" about work done that same week, then admitted "those were all
recent tasks from this week, not last year. My bad." He was never told what day it was, so every
absolute date in his context had nothing to be measured against.

Everything here is deterministic. A frozen clock gives a fixed reference instant, so every assertion is
reproducible and none of them depend on a model's sense of time — which is the whole point: these are
the questions that must never be answered by intuition.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sali.core.clock import FrozenClock
from sali.core.temporal import TemporalService

# 2026-09-03 17:43 UTC is 20:43 the same day in Dar es Salaam (UTC+3). Chosen deliberately: it is late
# enough in the owner's evening that his calendar day and the UTC day disagree for part of the window,
# which is exactly where naive "today" logic breaks.
NOW = datetime(2026, 9, 3, 17, 43, tzinfo=UTC)
OWNER_TZ = "Africa/Dar_es_Salaam"


@pytest.fixture
def clock() -> TemporalService:
    return TemporalService(FrozenClock(NOW), owner_timezone=OWNER_TZ)


# ── the canonical instant ───────────────────────────────────────────────────────────────────────────

def test_now_is_always_aware_utc(clock: TemporalService) -> None:
    """A naive datetime read as UTC when it was local shifts history by hours, and nothing downstream
    can tell. There is no code path here that produces one."""
    now = clock.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_the_owners_day_is_not_utcs_day(clock: TemporalService) -> None:
    """At 17:43 UTC it is already the evening of the same date in Dar — but three hours of every day
    fall on different calendar dates in the two zones, and "yesterday" must mean HIS yesterday."""
    assert clock.today().isoformat() == "2026-09-03"
    late = TemporalService(FrozenClock(datetime(2026, 9, 3, 22, 30, tzinfo=UTC)),
                           owner_timezone=OWNER_TZ)
    assert late.now().date().isoformat() == "2026-09-03", "UTC still says the 3rd"
    assert late.today().isoformat() == "2026-09-04", "but it is already the 4th where Almir is"


def test_a_misconfigured_zone_falls_back_to_utc_and_says_so() -> None:
    """It must never silently become the host's zone — that is the seven-hour error this prevents."""
    service = TemporalService(FrozenClock(NOW), owner_timezone="Mars/Olympus_Mons")
    assert service.zone_name() == "UTC"
    assert "unknown" in service.zone_source()


# ── §43 the false-premise test: how long ago was it, really ─────────────────────────────────────────

@pytest.mark.parametrize(("delta", "expected"), [
    (timedelta(seconds=30), "a moment ago"),
    (timedelta(minutes=5), "5 minutes ago"),
    (timedelta(hours=1), "1 hour ago"),
    (timedelta(hours=5), "earlier today"),
    (timedelta(hours=26), "yesterday"),
    (timedelta(days=3), "3 days ago"),
    (timedelta(days=9), "last week"),
    (timedelta(days=25), "3 weeks ago"),
    (timedelta(days=90), "3 months ago"),
    (timedelta(days=240), "8 months ago"),
    (timedelta(days=400), "about a year ago"),
])
def test_elapsed_time_gets_the_words_a_person_would_use(
    clock: TemporalService, delta: timedelta, expected: str,
) -> None:
    assert clock.ago(NOW - delta) == expected


def test_something_from_today_is_never_described_as_last_year(clock: TemporalService) -> None:
    """The exact failure Almir caught, as an assertion."""
    for hours in (0, 1, 4, 8, 12):
        phrase = clock.ago(NOW - timedelta(hours=hours))
        assert "year" not in phrase and "month" not in phrase and "week" not in phrase, phrase


def test_something_from_eight_months_ago_is_never_described_as_recent(clock: TemporalService) -> None:
    assert clock.ago(NOW - timedelta(days=240)) == "8 months ago"


def test_an_unknown_time_is_said_plainly_rather_than_guessed(clock: TemporalService) -> None:
    """§19/§46: no temporal evidence must produce an admission, never an invented date."""
    assert clock.ago(None) == "at an unknown time"


def test_a_future_instant_is_not_described_as_past(clock: TemporalService) -> None:
    """§47: a scheduled thing has not happened yet."""
    assert clock.ago(NOW + timedelta(hours=3)).startswith("in ")


# ── durations ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("seconds", "expected"), [
    (45, "45s"), (200, "3m"), (3 * 3600 + 21 * 60, "3h 21m"), (2 * 86400, "2 days"),
])
def test_durations_read_the_way_people_say_them(seconds: int, expected: str) -> None:
    assert TemporalService.duration(timedelta(seconds=seconds)) == expected


def test_a_task_running_for_three_hours_knows_it(clock: TemporalService) -> None:
    started = NOW - timedelta(hours=3, minutes=21)
    assert clock.duration(clock.elapsed(started)) == "3h 21m"


# ── deadlines ───────────────────────────────────────────────────────────────────────────────────────

def test_a_deadline_that_passed_is_overdue(clock: TemporalService) -> None:
    assert clock.is_overdue(NOW - timedelta(hours=2)) is True
    assert clock.is_overdue(NOW + timedelta(hours=2)) is False
    assert clock.is_overdue(None) is False, "no deadline is not an overdue deadline"


def test_due_soon_is_a_window_not_a_guess(clock: TemporalService) -> None:
    assert clock.is_due_within(NOW + timedelta(hours=2), timedelta(hours=3)) is True
    assert clock.is_due_within(NOW + timedelta(hours=9), timedelta(hours=3)) is False
    assert clock.is_due_within(NOW - timedelta(hours=1), timedelta(hours=3)) is False, "overdue ≠ due"


# ── §9 resolving what a person actually says ────────────────────────────────────────────────────────

def test_yesterday_is_the_owners_calendar_day_expressed_in_utc(clock: TemporalService) -> None:
    """Dar is UTC+3, so his 2nd of September runs from 21:00Z on the 1st to 21:00Z on the 2nd. A
    UTC-day implementation would return the wrong 24 hours and quietly search the wrong window."""
    span = clock.resolve("what did we discuss yesterday")
    assert span is not None
    assert span.start == datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
    assert span.end == datetime(2026, 9, 2, 21, 0, tzinfo=UTC)
    assert span.label == "yesterday"


@pytest.mark.parametrize("phrase", [
    "today", "tomorrow", "this morning", "last night", "three days ago", "in 20 minutes",
    "for the last 3 hours", "last week", "last month", "last year", "next monday", "last friday",
])
def test_the_phrases_he_uses_all_resolve(clock: TemporalService, phrase: str) -> None:
    span = clock.resolve(phrase)
    assert span is not None, f"{phrase!r} resolved to nothing"
    assert span.start < span.end, f"{phrase!r} produced an empty or inverted range"
    assert span.start.tzinfo is not None and span.end.tzinfo is not None


@pytest.mark.parametrize("phrase", ["how are you", "write me a python script", "", "thanks"])
def test_text_that_names_no_time_resolves_to_nothing(clock: TemporalService, phrase: str) -> None:
    """Silence must mean "no temporal constraint", never "now" — defaulting to now would quietly
    filter a search that was meant to be open."""
    assert clock.resolve(phrase) is None


def test_a_future_window_never_reaches_backwards(clock: TemporalService) -> None:
    span = clock.resolve("in 20 minutes")
    assert span is not None and span.start >= NOW


def test_last_year_is_the_whole_of_last_year(clock: TemporalService) -> None:
    span = clock.resolve("last year")
    assert span is not None
    assert span.start.year == 2024 and span.end.year == 2025, "2025 in Dar starts late on 2024-12-31"
    assert (span.end - span.start).days >= 365


# ── §5 what the model is told ───────────────────────────────────────────────────────────────────────

def test_the_turn_carries_the_instant_and_the_owners_civil_time(clock: TemporalService) -> None:
    block = clock.context_block()
    assert "2026-09-03 17:43 UTC" in block
    assert "20:43" in block and "Africa/Dar_es_Salaam" in block
    assert "Thursday" in block, "the weekday matters for 'by Friday'"


def test_a_gap_since_the_last_message_is_stated(clock: TemporalService) -> None:
    """§21: a conversation resumed after three days is a continuation, and nothing said so."""
    block = clock.context_block(last_turn_at=NOW - timedelta(days=3))
    assert "3 days ago" in block and "continuation" in block


def test_no_gap_line_when_he_just_spoke(clock: TemporalService) -> None:
    """Every token here is cache budget; a gap of seconds is not worth a line."""
    assert "continuation" not in clock.context_block(last_turn_at=NOW - timedelta(minutes=2))


# ── §34 is the clock trustworthy ────────────────────────────────────────────────────────────────────

def test_clock_health_reports_what_it_knows_and_admits_what_it_does_not() -> None:
    health = TemporalService(owner_timezone=OWNER_TZ).health()
    assert health["owner_timezone"] == OWNER_TZ
    assert health["timezone_source"] == "configured"
    assert "utc_now" in health
    assert health["synchronized"] in (True, False, None), "None is honest when it cannot be read"


# ── §1/§33 the codebase must not reintroduce naive time ─────────────────────────────────────────────

def test_no_naive_clock_calls_anywhere_in_the_source() -> None:
    """A guard, not a formality. `datetime.utcnow()` returns a NAIVE datetime that looks like UTC and
    compares wrongly against every aware timestamp in this system; `datetime.today()` is local. Both
    are currently absent and must stay that way."""
    root = Path(__file__).resolve().parent.parent / "src" / "sali"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in ("datetime.utcnow(", "datetime.today(", "datetime.now()"):
            if pattern in text:
                offenders.append(f"{path.relative_to(root)}: {pattern}")
    assert not offenders, "naive or local clock calls: " + "; ".join(offenders)


def test_every_database_timestamp_is_timezone_aware() -> None:
    """`timestamp without time zone` is an instant with no meaning. There are none, and adding one
    would silently reinterpret every row written after it."""
    probe = subprocess.run(
        ["psql", "-tAd", "sali", "-c",
         "SELECT count(*) FROM information_schema.columns "
         "WHERE table_schema='sali' AND data_type='timestamp without time zone'"],
        capture_output=True, text=True, timeout=15)
    if probe.returncode != 0:
        pytest.skip("postgres not reachable")
    assert probe.stdout.strip() == "0", "a naive timestamp column exists"


@pytest.mark.parametrize("phrase", [
    "good morning", "i had a good evening", "lunch options", "remind me at noon",
    "good night", "morning!",
])
def test_a_bare_part_of_day_word_is_a_greeting_not_a_time_filter(
    clock: TemporalService, phrase: str,
) -> None:
    """A resolved window NARROWS what Sali can recall, so an incidental word must never produce one.

    Shipped and caught within the hour: the determiner was optional, so "good morning" resolved to a
    six-hour window, the retrieval filter cut the memory pool down to that window, and Sali answered a
    greeting with no memories at all. Amnesia triggered by saying hello."""
    assert clock.resolve(phrase) is None


@pytest.mark.parametrize(("phrase", "expected_day_offset"), [
    ("this morning", 0),
    ("yesterday evening", -1),
    ("tonight", 0),
])
def test_a_part_of_day_with_a_determiner_still_resolves(
    clock: TemporalService, phrase: str, expected_day_offset: int,
) -> None:
    span = clock.resolve(phrase)
    assert span is not None
    assert span.start.date() >= (NOW + timedelta(days=expected_day_offset)).date() - timedelta(days=1)
