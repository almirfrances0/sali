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
    status: str = "pending"  # pending | running | waiting | blocked | done | failed | skipped
    note: str | None = None
    attempts: int = 0  # how many times this step has failed (§9 — the retry driver reads this)
    last_error: str | None = None
    failure_class: str | None = None  # sali.core.errors.FailureClass value, set on failure
    verified: bool = False  # true once a tool_execution verified the step's effect (§8)
    verified_by: UUID | None = None  # → tool_execution.id
    depends_on: list[int] = field(default_factory=list)  # step seqs that must finish first (§3-6 DAG)
    checkpoint: dict[str, Any] | None = None  # in-step progress → resume mid-step, not from scratch


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
        """The first READY step — pending/running with EVERY dependency already done/skipped (§3-6).
        Steps still waiting on an unfinished dependency are not 'next' yet: this is a per-step DAG,
        not strict sequential order, so Sali never jumps a prerequisite."""
        finished = {s.seq for s in self.steps if s.status in ("done", "skipped")}
        return next(
            (s for s in self.steps
             if s.status in ("pending", "running", "waiting") and all(d in finished for d in s.depends_on)),
            None,
        )

    @property
    def blocked_steps(self) -> list[TaskStep]:
        """Not-yet-done steps whose dependencies aren't all satisfied — waiting on a prerequisite."""
        finished = {s.seq for s in self.steps if s.status in ("done", "skipped")}
        return [
            s for s in self.steps
            if s.status in ("pending", "waiting", "blocked") and not all(d in finished for d in s.depends_on)
        ]

    def one_line(self) -> str:
        """A compact resume view for the context section: objective + step ticks + what's next, plus a
        mid-step checkpoint hint (§5) so Sali resumes where it actually got to, and a dependency-wait note."""
        ticks = " ".join(f"{s.seq}.{_TICK.get(s.status, '·')}" for s in self.steps)
        nxt = self.next_step
        if nxt is not None:
            tail = f" — next: step {nxt.seq} ({nxt.description})"
            if nxt.checkpoint:
                progress = ", ".join(f"{k}={v}" for k, v in list(nxt.checkpoint.items())[:3])
                tail += f" [resuming mid-step: {progress}]"
        elif self.blocked_steps:
            tail = " — waiting on dependencies"
        else:
            tail = ""
        return f"{self.objective} [{ticks}]{tail}"


_TICK = {"done": "✓", "failed": "✗", "running": "▷", "skipped": "–", "pending": "·",
         "waiting": "⋯", "blocked": "⊘"}


def _col(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that may be absent from older/partial rows (keeps row_to_task robust)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def row_to_task(task_row: Any, step_rows: list[Any]) -> Task:
    return Task(
        id=task_row["id"],
        objective=task_row["objective"],
        status=task_row["status"],
        result=task_row["result"],
        created_at=task_row["created_at"],
        updated_at=task_row["updated_at"],
        steps=[
            TaskStep(
                seq=r["seq"],
                description=r["description"],
                status=r["status"],
                note=r["note"],
                attempts=_col(r, "attempts", 0),
                last_error=_col(r, "last_error"),
                failure_class=_col(r, "failure_class"),
                verified=_col(r, "verified", False),
                verified_by=_col(r, "verified_by"),
                depends_on=list(_col(r, "depends_on", []) or []),
                checkpoint=_col(r, "checkpoint"),
            )
            for r in step_rows
        ],
    )
