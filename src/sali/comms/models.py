"""Communications value objects — email + calendar (§44)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class EmailMessage:
    uid: str
    sender: str
    subject: str
    date: str
    body: str = ""  # populated on read(); a search returns headers only

    def summary(self) -> str:
        return f"[{self.uid}] {self.date} — {self.sender}: {self.subject}"


@dataclass(slots=True)
class CalendarEvent:
    uid: str
    summary: str
    start: datetime
    end: datetime | None = None
    location: str | None = None

    def one_line(self) -> str:
        when = self.start.strftime("%Y-%m-%d %H:%M")
        where = f" @ {self.location}" if self.location else ""
        return f"{when} — {self.summary}{where}"
