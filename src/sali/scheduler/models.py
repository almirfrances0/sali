"""Schedule value object (§44)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class Schedule:
    id: UUID
    name: str
    kind: str  # cron | interval
    spec: str
    prompt: str
    enabled: bool
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None
    # The zone a cron spec's fields are civil time in. Irrelevant for intervals, which are durations.
    timezone: str = "UTC"

    def one_line(self) -> str:
        state = "" if self.enabled else " (paused)"
        when = f"{self.spec}" if self.kind == "cron" else f"every {self.spec}"
        return f"{self.name}{state}: {when} → {self.prompt}"


def row_to_schedule(row: Any) -> Schedule:
    return Schedule(
        id=row["id"], name=row["name"], kind=row["kind"], spec=row["spec"], prompt=row["prompt"],
        enabled=row["enabled"], next_run_at=row["next_run_at"], last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        # Kept on the model so a schedule can be shown as the civil time it actually is. Without it a
        # UI can only print a UTC instant, and "fires at 06:00" is a confusing way to describe the
        # 9am alarm someone asked for.
        timezone=(row["timezone"] if "timezone" in row.keys() else "UTC"))
