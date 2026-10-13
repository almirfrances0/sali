"""Calendar over CalDAV (§44). Uses httpx (already a dep) with basic auth (username + app-password
from the SecretStore). icalendar is OPTIONAL: present → robust VEVENT parsing; absent → a minimal
line parser handles SUMMARY/DTSTART so list_events still works, and add_event builds ICS by hand.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from sali.comms.models import CalendarEvent

_TIMEOUT = 20.0
_CAL_DATA = re.compile(r"BEGIN:VEVENT.*?END:VEVENT", re.DOTALL)


@dataclass(slots=True)
class CalAccount:
    url: str  # the calendar collection URL
    username: str
    password: str  # resolved from SecretStore just before use; never persisted


class CalClient(Protocol):
    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...
    async def add_event(self, summary: str, start: datetime, end: datetime,
                        *, location: str | None = None) -> str: ...


class CalDavCalendar:
    def __init__(self, account: CalAccount) -> None:
        self._acct = account

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(auth=(self._acct.username, self._acct.password), timeout=_TIMEOUT)

    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        body = (
            '<?xml version="1.0"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{_ical_dt(start)}" end="{_ical_dt(end)}"/>'
            "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
        )
        async with self._client() as client:
            resp = await client.request(
                "REPORT", self._acct.url,
                headers={"Depth": "1", "Content-Type": "application/xml"}, content=body)
        resp.raise_for_status()
        return [ev for block in _CAL_DATA.findall(resp.text) if (ev := _parse_vevent(block))]

    async def add_event(self, summary: str, start: datetime, end: datetime,
                        *, location: str | None = None) -> str:
        uid = hashlib.sha256(f"{summary}{start.isoformat()}".encode()).hexdigest()[:32] + "@sali"
        ics = _build_ics(uid, summary, start, end, location)
        href = self._acct.url.rstrip("/") + f"/{uid}.ics"
        async with self._client() as client:
            resp = await client.put(href, headers={"Content-Type": "text/calendar"}, content=ics)
        resp.raise_for_status()
        return uid


class FakeCalClient:
    """In-memory calendar for CI."""

    def __init__(self, events: list[CalendarEvent] | None = None) -> None:
        self.events = list(events or [])
        self.added: list[CalendarEvent] = []

    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [e for e in self.events if start <= e.start <= end]

    async def add_event(self, summary: str, start: datetime, end: datetime,
                        *, location: str | None = None) -> str:
        ev = CalendarEvent(uid=f"fake-{len(self.added)}", summary=summary, start=start, end=end,
                           location=location)
        self.added.append(ev)
        self.events.append(ev)
        return ev.uid


def _ical_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _build_ics(uid: str, summary: str, start: datetime, end: datetime, location: str | None) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Sali//EN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTART:{_ical_dt(start)}", f"DTEND:{_ical_dt(end)}",
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def _parse_vevent(block: str) -> CalendarEvent | None:
    try:
        import icalendar  # optional — robust parse when present
        cal = icalendar.Calendar.from_ical("BEGIN:VCALENDAR\r\n" + block + "\r\nEND:VCALENDAR")
        for comp in cal.walk("VEVENT"):
            start = comp.get("DTSTART").dt
            end_prop = comp.get("DTEND")
            return CalendarEvent(
                uid=str(comp.get("UID", "")), summary=str(comp.get("SUMMARY", "")),
                start=_as_dt(start), end=_as_dt(end_prop.dt) if end_prop else None,
                location=str(comp.get("LOCATION")) if comp.get("LOCATION") else None)
    except ImportError:
        return _parse_vevent_minimal(block)
    except Exception:  # noqa: BLE001 - a malformed VEVENT is skipped, not fatal
        return None
    return None


def _parse_vevent_minimal(block: str) -> CalendarEvent | None:
    fields = dict(re.findall(r"^([A-Z]+)(?:;[^:]*)?:(.*)$", block, re.MULTILINE))
    start_raw = fields.get("DTSTART")
    if not start_raw:
        return None
    try:
        start = datetime.strptime(start_raw[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return CalendarEvent(uid=fields.get("UID", ""), summary=fields.get("SUMMARY", ""),
                         start=start, location=fields.get("LOCATION") or None)


def _as_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(getattr(value, "year", 1970), getattr(value, "month", 1),
                    getattr(value, "day", 1))
