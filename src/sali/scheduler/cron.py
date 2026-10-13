"""A self-contained schedule engine — cron + intervals, no external dependency.

Sali is local-first and must not depend on croniter (uncertain on py3.14); this computes the next
fire time deterministically. Two kinds:
  - interval: "30s" / "15m" / "2h" / "1d" → fire that long after the last run.
  - cron: a standard 5-field spec "min hour dom mon dow" with * , lists (1,2,3), ranges (1-5), and
    steps (*/15). Day-of-month / day-of-week follow cron's OR rule when both are restricted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))  # min, hour, dom, month, dow
_INTERVAL = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class ScheduleError(ValueError):
    """A schedule spec that doesn't parse — surfaced to Sali/Almir, never a silent misfire."""


def parse_when(when: str) -> tuple[str, str]:
    """Turn a friendly 'when' into (kind, spec). '30m'/'2h' → interval; a 5-field string → cron.
    Raises ScheduleError on anything that isn't a valid interval or cron so a typo can't create a
    schedule that never fires (or fires wrong)."""
    text = when.strip()
    if _INTERVAL.match(text):
        return "interval", text.lower().replace(" ", "")
    if len(text.split()) == 5:
        _parse_cron(text)  # validate now — raises if malformed
        return "cron", text
    raise ScheduleError(
        f"'{when}' isn't a valid schedule — use an interval like '30m'/'2h'/'1d' or a 5-field cron "
        "like '0 9 * * *'")


def next_run(kind: str, spec: str, after: datetime, tz: str | None = None) -> datetime:
    """The first fire time strictly after ``after``, as an aware UTC instant.

    A CRON SPEC IS CIVIL TIME, NOT UTC. "0 9 * * *" means nine in the morning where the person lives —
    and this system's owner is in Africa/Dar_es_Salaam while the host it runs on is set to
    America/New_York, so evaluating the spec against UTC put every daily reminder three hours late and
    every host-local one seven hours out. The fields are therefore matched in ``tz`` and only the
    ANSWER is converted back, which is also what makes it survive DST: the schedule stays at 09:00
    local across a transition instead of silently sliding an hour, because the offset is re-derived at
    each firing rather than baked in once.

    An INTERVAL is a duration, not a civil time — "every 30m" means every thirty minutes wherever you
    are — so it is unaffected by the zone and deliberately does not consult it."""
    if kind == "interval":
        return after + _interval_delta(spec)
    if kind == "cron":
        if tz is None or tz == "UTC":
            return _next_cron(spec, after)
        zone = ZoneInfo(tz)
        local_after = after.astimezone(zone)
        # Matched naively inside the zone: the walk below steps minute by minute over civil fields,
        # and an aware arithmetic there would drag the offset along with it. Re-localising the result
        # is what applies the correct offset for the day the schedule actually lands on.
        local_next = _next_cron(spec, local_after.replace(tzinfo=None))
        return local_next.replace(tzinfo=zone).astimezone(UTC)
    raise ScheduleError(f"unknown schedule kind {kind!r}")


def _interval_delta(spec: str) -> timedelta:
    m = _INTERVAL.match(spec)
    if not m:
        raise ScheduleError(f"bad interval {spec!r}")
    value, unit = int(m.group(1)), m.group(2).lower()
    if value <= 0:
        raise ScheduleError("an interval must be positive")
    return timedelta(**{_UNIT[unit]: value})


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        piece, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step <= 0:
            raise ScheduleError(f"bad step in {field!r}")
        if piece in ("*", ""):
            start, end = lo, hi
        elif "-" in piece:
            a, b = piece.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(piece)
        if start < lo or end > hi or start > end:
            raise ScheduleError(f"field {field!r} out of range [{lo},{hi}]")
        out.update(range(start, end + 1, step))
    return out


def _parse_cron(spec: str) -> tuple[set[int], ...]:
    fields = spec.split()
    if len(fields) != 5:
        raise ScheduleError("a cron spec has exactly 5 fields: min hour dom month dow")
    # cron allows dow 7 as Sunday; normalise to 0.
    parsed = [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_BOUNDS, strict=True)]
    if 7 in parsed[4]:
        parsed[4].discard(7)
        parsed[4].add(0)
    return tuple(parsed)


def _day_matches(day: datetime, doms: set[int], dows: set[int], *, dom_star: bool, dow_star: bool) -> bool:
    dom_ok = day.day in doms
    dow_ok = (day.isoweekday() % 7) in dows  # cron dow: Sun=0 .. Sat=6
    if not dom_star and not dow_star:
        return dom_ok or dow_ok  # cron's OR rule when BOTH are restricted
    if not dom_star:
        return dom_ok
    if not dow_star:
        return dow_ok
    return True


def _next_cron(spec: str, after: datetime) -> datetime:
    fields = spec.split()
    mins, hrs, doms, mons, dows = _parse_cron(spec)
    dom_star, dow_star = fields[2] == "*", fields[4] == "*"
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 5):  # scan up to ~5 years of days; guards an impossible spec
        if t.month in mons and _day_matches(t, doms, dows, dom_star=dom_star, dow_star=dow_star):
            for hm in range(t.hour * 60 + t.minute, 24 * 60):
                h, m = divmod(hm, 60)
                if h in hrs and m in mins:
                    return t.replace(hour=h, minute=m)
        t = (t + timedelta(days=1)).replace(hour=0, minute=0)
    raise ScheduleError(f"cron {spec!r} has no next fire time within 5 years")
