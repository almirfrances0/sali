"""Task value objects (spec §24)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class TaskStep:
    seq: int
    description: str
    status: str = "pending"  # pending | running | done | failed | skipped
    note: str | None = None


@dataclass(slots=True)
class Task:
    id: UUID
    objective: str
    status: str  # open | running | done | failed | abandoned
    steps: list[TaskStep] = field(default_factory=list)
    result: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def next_step(self) -> TaskStep | None:
        """The first step not yet finished — what Sali should work on to resume."""
        return next((s for s in self.steps if s.status in ("pending", "running")), None)

    def one_line(self) -> str:
        """A compact resume view for the context section: objective + step ticks + what's next."""
        ticks = " ".join(f"{s.seq}.{_TICK.get(s.status, '·')}" for s in self.steps)
        nxt = self.next_step
        tail = f" — next: step {nxt.seq} ({nxt.description})" if nxt else ""
        return f"{self.objective} [{ticks}]{tail}"


_TICK = {"done": "✓", "failed": "✗", "running": "▷", "skipped": "–", "pending": "·"}


def row_to_task(task_row: Any, step_rows: list[Any]) -> Task:
    return Task(
        id=task_row["id"],
        objective=task_row["objective"],
        status=task_row["status"],
        result=task_row["result"],
        created_at=task_row["created_at"],
        updated_at=task_row["updated_at"],
        steps=[
            TaskStep(seq=r["seq"], description=r["description"], status=r["status"], note=r["note"])
            for r in step_rows
        ],
    )
