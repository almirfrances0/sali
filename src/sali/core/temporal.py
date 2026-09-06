"""Sali's single temporal authority: where "now" comes from, and where time becomes words.

Sali's timestamps were never the problem — the database has no naive column in it, and `core.clock`
has provided an injectable UTC clock from the start. What was missing is everything ABOVE the
timestamp. The model was never told the current date, so an absolute "2026-09-03" in the prompt had no
reference point to be measured against, and he filled the gap by guessing: on 2026-09-03 he told Almir
they had been working together "since last year (August 2024)" about work done that same week.

So this module is deliberately not a clock. It is the layer that turns instants into the things a
person actually reasons with — how long ago, how long for, is it overdue, what does "yesterday" mean —
and it does all of it deterministically, because none of these questions should ever be answered by a
language model's intuition.

THREE IDEAS KEPT SEPARATE, because collapsing them is where temporal bugs come from:

* **The canonical instant** is UTC, always aware, and is what every stored timestamp means.
* **Civil time** is what a person means by "9am" or "yesterday". Day boundaries are LOCAL: at 22:00 in
  Dar es Salaam it is already tomorrow in UTC, so a UTC-based "today" would answer the wrong question.
  Every calendar judgement here therefore runs in the owner's zone and only the result comes back UTC.
* **Elapsed time** is a duration, and for anything measured inside one process it belongs to a
  monotonic clock, which `core.clock` already owns. Wall-clock subtraction is correct for "when did
  this happen" and wrong for "how long has this been running" the moment NTP steps the clock.

THE ZONE PROBLEM THIS SYSTEM ACTUALLY HAS. Sali lives on a host set to America/New_York; Almir works in
Africa/Dar_es_Salaam. Seven hours apart. Nothing in the codebase had any notion of a timezone, so the
host's zone would have silently become the answer to "9am" — off by seven hours, every time. The
owner's zone is therefore configuration, not a guess from the host, and `zone_source()` reports where
the answer came from so Sali can say what he is assuming instead of pretending to know.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sali.core.clock import Clock, SystemClock

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

# Parts of the day, in local civil hours. Rough on purpose: these are how people speak, not definitions.
_DAY_PARTS = {
    "morning": (5, 12), "afternoon": (12, 17), "evening": (17, 22),
    "tonight": (18, 23), "night": (20, 24), "midday": (11, 14), "noon": (11, 14),
    "lunch": (12, 14), "lunchtime": (12, 14),
}


@dataclass(slots=True, frozen=True)
class TimeRange:
    """A resolved stretch of time, half-open [start, end), always UTC.

    A range rather than an instant because almost every human time expression IS one: "yesterday" is a
    day, "last week" is a week, "this morning" is a few hours. Collapsing them to a point is what makes
    a search for "what did we discuss yesterday" return whatever happened to be newest."""

    start: datetime
    end: datetime
    label: str

    def contains(self, moment: datetime) -> bool:
        return self.start <= _as_utc(moment) < self.end


def _as_utc(moment: datetime) -> datetime:
    """Any datetime, as an aware UTC instant.

    A naive datetime is the one thing this module refuses to guess about: reading it as UTC when it was
    local (or the reverse) silently shifts history by hours, and nothing downstream can tell. Naive
    input is treated as UTC because that is what every column in this database stores — but it is a
    conversion, not an interpretation, and it should never be reached from stored data."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class TemporalService:
    """The one place that answers "what time is it" and "how long ago was that"."""

    def __init__(self, clock: Clock | None = None, *, owner_timezone: str | None = None) -> None:
        self._clock = clock or SystemClock()
        self._configured_zone = owner_timezone
        self._zone_source = "configured" if owner_timezone else "default"
        self._zone = self._resolve_zone(owner_timezone)

    def _resolve_zone(self, name: str | None) -> ZoneInfo:
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                # A misconfigured zone must not take the whole service down, and must not silently
                # become the host's zone either — that is the seven-hour error this exists to prevent.
                self._zone_source = "default (configured zone unknown)"
        return ZoneInfo("UTC")

    # ── the canonical instant ────────────────────────────────────────────────────────────────────
    def now(self) -> datetime:
        """The current instant, aware, UTC. Everything else here is derived from this one call."""
        return _as_utc(self._clock.now())

    def zone(self) -> ZoneInfo:
        return self._zone

    def zone_name(self) -> str:
        return str(self._zone.key)

    def zone_source(self) -> str:
        """Where the owner's timezone came from — so Sali can say what he is assuming."""
        return self._zone_source

    def local(self, moment: datetime | None = None) -> datetime:
        """An instant as the owner would read it off a wall clock."""
        return _as_utc(moment or self.now()).astimezone(self._zone)

    def today(self) -> date:
        """The owner's current calendar day, not UTC's. These differ for hours every day."""
        return self.local().date()

    # ── owner's civil-day boundaries ─────────────────────────────────────────────────────────────
    def start_of_civil_day(self, moment: datetime | None = None) -> datetime:
        """Midnight in the owner's zone for the calendar day of `moment` (defaults to now), as UTC.

        Used by the agenda synthesiser to answer "today" against the owner's clock, not the host's.
        Almir at UTC+3: "today's schedules" ends at 21:00 UTC, not midnight UTC. Computed by
        localising to the zone, replacing the time-of-day components, and converting back to UTC -
        so a DST transition inside the day is handled by ZoneInfo, not by arithmetic on the offset."""
        m = self.now() if moment is None else _as_utc(moment)
        local = m.astimezone(self._zone).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.astimezone(UTC)

    def end_of_civil_day(self, moment: datetime | None = None) -> datetime:
        """The final microsecond of the owner's calendar day for `moment`, as UTC.

        Uses start_of(next-day) - 1us rather than 23:59:59 so a DST spring-forward doesn't lose an
        hour and a DST fall-back doesn't include the repeated hour of the following day."""
        start = self.start_of_civil_day(moment)
        # +25h then back to start-of-day handles the ~1h DST spring-forward and the ~1h fall-back
        # transparently - the ZoneInfo does the boundary work, we just ask for the next day's start.
        next_day_start = self.start_of_civil_day(start + timedelta(hours=25))
        return next_day_start - timedelta(microseconds=1)

    # ── durations ────────────────────────────────────────────────────────────────────────────────
    def elapsed(self, since: datetime, *, until: datetime | None = None) -> timedelta:
        return (_as_utc(until) if until else self.now()) - _as_utc(since)

    def is_overdue(self, deadline: datetime | None) -> bool:
        return deadline is not None and self.now() > _as_utc(deadline)

    def is_due_within(self, deadline: datetime | None, window: timedelta) -> bool:
        if deadline is None:
            return False
        remaining = _as_utc(deadline) - self.now()
        return timedelta(0) <= remaining <= window

    # ── time as words ────────────────────────────────────────────────────────────────────────────
    def ago(self, moment: datetime | None) -> str:
        """How long ago, in the words a person would use.

        Thresholds rather than a formatter, because the point is to stop the model choosing the word.
        "Recently" for something eight months old and "last year" for something from Tuesday are the
        two failures this replaces, and both come from a model reasoning about a bare date."""
        if moment is None:
            return "at an unknown time"
        delta = self.elapsed(moment)
        seconds = delta.total_seconds()
        if seconds < 0:
            return self._ahead(-seconds)
        if seconds < 90:
            return "a moment ago"
        if seconds < _HOUR:
            minutes = int(seconds // _MINUTE)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"

        # Past an hour, calendar position says more than arithmetic does: "earlier today" is more
        # useful than "7 hours ago", and whether something was yesterday depends on the owner's
        # midnight, not on a 24-hour subtraction.
        local_then, local_now = self.local(moment), self.local()
        days_apart = (local_now.date() - local_then.date()).days
        if days_apart == 0:
            hours = int(seconds // _HOUR)
            return "earlier today" if hours >= 3 else f"{hours} hour{'s' if hours != 1 else ''} ago"
        if days_apart == 1:
            return "yesterday"
        if days_apart < 7:
            return f"{days_apart} days ago"
        if days_apart < 14:
            return "last week"
        if days_apart < 31:
            weeks = days_apart // 7
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        if days_apart < 365:
            months = max(1, round(days_apart / 30.44))
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days_apart / 365.25
        if years < 1.5:
            return "about a year ago"
        return f"{years:.0f} years ago"

    @staticmethod
    def _ahead(seconds: float) -> str:
        if seconds < 90:
            return "in a moment"
        if seconds < _HOUR:
            return f"in {int(seconds // _MINUTE)} minutes"
        if seconds < _DAY:
            return f"in {int(seconds // _HOUR)} hours"
        return f"in {int(seconds // _DAY)} days"

    @staticmethod
    def duration(delta: timedelta | float) -> str:
        """A length of time, the way a person says it: "3h 21m", "2 days", "45s"."""
        seconds = int(delta.total_seconds() if isinstance(delta, timedelta) else delta)
        if seconds < 0:
            return "no time at all"
        if seconds < _MINUTE:
            return f"{seconds}s"
        if seconds < _HOUR:
            return f"{seconds // _MINUTE}m"
        if seconds < _DAY:
            hours, minutes = seconds // _HOUR, (seconds % _HOUR) // _MINUTE
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        days, hours = seconds // _DAY, (seconds % _DAY) // _HOUR
        if days < 7:
            return f"{days}d {hours}h" if hours else f"{days} day{'s' if days != 1 else ''}"
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''}"

    # ── human time expressions → real instants ───────────────────────────────────────────────────
    def resolve(self, text: str) -> TimeRange | None:
        """Turn a phrase like "yesterday" or "in two hours" into an actual span of UTC time.

        Returns None when the text names no time — silence means "no temporal constraint", never
        "now", because defaulting to now would quietly filter a search that was meant to be open."""
        if not text:
            return None
        lowered = " ".join(text.lower().split())
        for resolver in (self._resolve_named_day, self._resolve_part_of_day, self._resolve_offset,
                         self._resolve_period, self._resolve_weekday):
            found = resolver(lowered)
            if found is not None:
                return found
        return None

    def _local_day(self, day: date, *, label: str) -> TimeRange:
        """A whole civil day in the owner's zone, expressed as the UTC span it actually occupies."""
        start_local = datetime(day.year, day.month, day.day, tzinfo=self._zone)
        return TimeRange(_as_utc(start_local), _as_utc(start_local + timedelta(days=1)), label)

    def _resolve_named_day(self, text: str) -> TimeRange | None:
        today = self.today()
        if re.search(r"\btoday\b", text):
            return self._local_day(today, label="today")
        if re.search(r"\byesterday\b", text):
            return self._local_day(today - timedelta(days=1), label="yesterday")
        if re.search(r"\btomorrow\b", text):
            return self._local_day(today + timedelta(days=1), label="tomorrow")
        if re.search(r"\bday before yesterday\b", text):
            return self._local_day(today - timedelta(days=2), label="the day before yesterday")
        return None

    def _resolve_part_of_day(self, text: str) -> TimeRange | None:
        """"this morning", "yesterday evening" — but NEVER a bare part-of-day word.

        The determiner was optional here and it made "good morning" resolve to a six-hour window. Since
        a resolved window narrows what gets recalled, a greeting silently filtered the memory pool down
        to this morning and Sali answered with nothing — amnesia triggered by saying hello. "tonight"
        is the one word that carries its own determiner, so it stands alone."""
        match = re.search(r"\b(?:(this|last|yesterday|tomorrow)\s+(morning|afternoon|evening|night|"
                          r"midday|noon|lunchtime|lunch)|(tonight))\b", text)
        if match is None:
            return None
        part = match.group(2) or match.group(3)
        day = self.today()
        if "yesterday" in text or "last night" in text:
            day = day - timedelta(days=1)
        start_hour, end_hour = _DAY_PARTS[part]
        start_local = datetime(day.year, day.month, day.day, start_hour, tzinfo=self._zone)
        end_local = (datetime(day.year, day.month, day.day, tzinfo=self._zone)
                     + timedelta(hours=end_hour))
        return TimeRange(_as_utc(start_local), _as_utc(end_local), f"{part} on {day.isoformat()}")

    _UNITS = {
        "second": 1, "seconds": 1, "sec": 1, "secs": 1,
        "minute": _MINUTE, "minutes": _MINUTE, "min": _MINUTE, "mins": _MINUTE,
        "hour": _HOUR, "hours": _HOUR, "hr": _HOUR, "hrs": _HOUR,
        "day": _DAY, "days": _DAY,
        "week": 7 * _DAY, "weeks": 7 * _DAY,
        "month": 30 * _DAY, "months": 30 * _DAY,
        "year": 365 * _DAY, "years": 365 * _DAY,
    }
    _WORD_NUMBERS = {
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
        "couple": 2, "few": 3, "several": 4,
    }

    def _count(self, token: str) -> int | None:
        if token.isdigit():
            return int(token)
        return self._WORD_NUMBERS.get(token)

    def _resolve_offset(self, text: str) -> TimeRange | None:
        """"three days ago", "in 20 minutes", "for the last 3 hours", "since 2 days ago"."""
        now = self.now()
        past = re.search(r"\b(\d+|[a-z]+)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b", text)
        if past is not None:
            count = self._count(past.group(1))
            if count is not None:
                span = timedelta(seconds=count * self._UNITS[past.group(2)])
                moment = now - span
                # A point in the past is reported as the surrounding hour, not an instant: "three days
                # ago" does not mean 14:07 three days ago, and a zero-width range matches nothing.
                return TimeRange(moment - timedelta(hours=1), moment + timedelta(hours=1),
                                 f"{past.group(1)} {past.group(2)}s ago")
        window = re.search(r"\b(?:for the )?(?:last|past)\s+(\d+|[a-z]+)\s+"
                           r"(second|minute|hour|day|week|month|year)s?\b", text)
        if window is not None:
            count = self._count(window.group(1))
            if count is not None:
                span = timedelta(seconds=count * self._UNITS[window.group(2)])
                return TimeRange(now - span, now, f"the last {window.group(1)} {window.group(2)}s")
        future = re.search(r"\bin\s+(\d+|[a-z]+)\s+(second|minute|hour|day|week|month|year)s?\b", text)
        if future is not None:
            count = self._count(future.group(1))
            if count is not None:
                span = timedelta(seconds=count * self._UNITS[future.group(2)])
                moment = now + span
                # Clamped at `now`: a window around a future point must never reach backwards, or
                # "in 20 minutes" would match things that have already happened.
                return TimeRange(max(now, moment - timedelta(minutes=30)),
                                 moment + timedelta(minutes=30),
                                 f"in {future.group(1)} {future.group(2)}s")
        return None

    def _resolve_period(self, text: str) -> TimeRange | None:
        """"last week", "this month", "next year" — whole calendar periods in the owner's zone."""
        match = re.search(r"\b(last|this|next|past)\s+(week|month|year)\b", text)
        if match is None:
            return None
        which, unit = match.group(1), match.group(2)
        today = self.today()
        if unit == "week":
            monday = today - timedelta(days=today.weekday())
            start = monday - timedelta(weeks=1) if which in ("last", "past") else (
                monday + timedelta(weeks=1) if which == "next" else monday)
            return TimeRange(_as_utc(datetime(start.year, start.month, start.day, tzinfo=self._zone)),
                             _as_utc(datetime(start.year, start.month, start.day, tzinfo=self._zone)
                                     + timedelta(weeks=1)), f"{which} week")
        if unit == "month":
            year, month = today.year, today.month
            if which in ("last", "past"):
                year, month = (year - 1, 12) if month == 1 else (year, month - 1)
            elif which == "next":
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            start_local = datetime(year, month, 1, tzinfo=self._zone)
            end_local = (datetime(year + 1, 1, 1, tzinfo=self._zone) if month == 12
                         else datetime(year, month + 1, 1, tzinfo=self._zone))
            return TimeRange(_as_utc(start_local), _as_utc(end_local), f"{which} month")
        year = today.year - 1 if which in ("last", "past") else (
            today.year + 1 if which == "next" else today.year)
        return TimeRange(_as_utc(datetime(year, 1, 1, tzinfo=self._zone)),
                         _as_utc(datetime(year + 1, 1, 1, tzinfo=self._zone)), f"{which} year")

    def _resolve_weekday(self, text: str) -> TimeRange | None:
        """"last friday", "next monday" — the nearest such day in that direction."""
        match = re.search(r"\b(last|next|this)\s+(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)"
                          r"(?:day|sday|nesday|rsday|urday)?\b", text)
        if match is None:
            return None
        which, target = match.group(1), _WEEKDAYS[match.group(2)]
        today = self.today()
        delta = (target - today.weekday()) % 7
        if which == "next":
            day = today + timedelta(days=delta or 7)
        elif which == "last":
            back = (today.weekday() - target) % 7
            day = today - timedelta(days=back or 7)
        else:
            day = today + timedelta(days=delta)
        return self._local_day(day, label=f"{which} {match.group(2)}")

    # ── what the model is told, every turn ───────────────────────────────────────────────────────
    def context_block(self, *, last_turn_at: datetime | None = None) -> str:
        """The temporal header for the prompt.

        Short by design. This sits in the prompt on every single turn, and the prompt has a measured
        cache budget, so it carries only what cannot be derived: the instant, the owner's civil time,
        the weekday, and — when there is one — how long since he last spoke. Everything else in this
        module exists to render other values relative to THIS, rather than to add more lines here."""
        now = self.now()
        local = self.local(now)
        lines = [
            f"Right now it is {now:%Y-%m-%d %H:%M} UTC "
            f"({local:%A %-d %B %Y, %H:%M} where Almir is, {self.zone_name()}).",
        ]
        if last_turn_at is not None:
            gap = self.elapsed(last_turn_at)
            if gap.total_seconds() > 15 * _MINUTE:
                lines.append(f"His previous message was {self.ago(last_turn_at)} — "
                             "this is a continuation after a gap, not a fresh start; pick up the thread with him naturally "
                             "rather than restarting cold.")
        return "\n".join(lines)

    # ── is the clock itself trustworthy ──────────────────────────────────────────────────────────
    _health_cache: dict[str, Any] | None = None
    _health_checked_at: float = 0.0
    _HEALTH_TTL_S = 600.0

    def health_cached(self) -> dict[str, Any]:
        """Clock health, at most once every ten minutes.

        `health()` shells out to timedatectl, which is far too expensive to pay on every turn to catch
        a condition that changes about never — but a wrong clock writes a false history that looks
        exactly like a true one, so it does need checking. Ten minutes is the compromise."""
        import time as _time

        now = _time.monotonic()
        if self._health_cache is None or (now - self._health_checked_at) > self._HEALTH_TTL_S:
            self._health_cache = self.health()
            self._health_checked_at = now
        return self._health_cache

    def health(self) -> dict[str, Any]:
        """Clock health from the OS's own time synchronisation, never a home-made one.

        A wrong clock does not announce itself: it writes a false history that looks exactly like a
        true one. Reading systemd-timesyncd's own verdict is both cheaper and more honest than trying
        to detect drift from inside the process."""
        state: dict[str, Any] = {"utc_now": self.now().isoformat(), "owner_timezone": self.zone_name(),
                                 "timezone_source": self.zone_source(), "synchronized": None}
        try:
            import subprocess

            # Deliberately NOT `--value`: timedatectl prints properties in its own order, not the
            # order asked for, so a positional read silently swaps the answers — it reported the
            # timezone as the sync state and "yes" as the timezone. KEY=VALUE cannot be misread.
            out = subprocess.run(
                ["timedatectl", "show", "--property=NTPSynchronized", "--property=Timezone"],
                capture_output=True, text=True, timeout=3.0)
            if out.returncode == 0:
                fields = dict(
                    line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
                if "NTPSynchronized" in fields:
                    state["synchronized"] = fields["NTPSynchronized"].strip().lower() in (
                        "yes", "true", "1")
                if "Timezone" in fields:
                    state["host_timezone"] = fields["Timezone"].strip()
        except Exception:  # noqa: BLE001 - health must never raise; unknown is an honest answer
            pass
        return state


__all__ = ["TemporalService", "TimeRange"]
