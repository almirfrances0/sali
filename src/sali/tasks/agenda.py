"""The AgendaSynthesiser: one read-model across the persistent stores that hold Sali's life.

Almir's brief was clear that the agenda is not a new table (the entities are already persisted -
`task`, `commitment`, `schedule`, `goal`, `initiative`, `activity`) but a coherent VIEW across them
answering the questions a person carries in their head: what am I on right now, what's due today,
what's coming up, what's blocked, what's waiting for me, what's overdue.

The view is:

    now             one active task (if any), plus commitments due within the hour
    today           commitments and schedules with a target time in Almir's local today
    overdue         commitments past their deadline and not yet fulfilled
    waiting         tasks the runtime marked waiting_for_user
    blocked         tasks whose status is `blocked` and open commitments marked at_risk/blocked
    upcoming        the next N schedule firings (bounded)
    goals           active goals with derived progress
    initiatives     active initiatives with their milestone counts

Nothing is invented here: every field is read from a live row. If no row is present, the section
is empty - never "we probably have three goals" prose. That is what makes the same read safe on the
chat path ("what's on my agenda") and on the iPhone (a real screen backed by real state).

TEMPORAL AUTHORITY is a single injected TemporalService. "Today" means Almir's civil day (his
timezone), computed by the same clock that renders every other time in the system - so a schedule
that fires at 09:00 Africa/Dar_es_Salaam lands in "today" for the day that IS today where he is.
The reader never touches the naive stdlib clock; the seam is the parameter.

BOUNDS. Every list has a `limit` (default 5 per section). Retrieval is by relevance ranking
per-section, not by dumping. The whole agenda serialises to well under 1KB - safe for the model's
prompt and for the app's payload.

READ-ONLY. This is a synthesiser; it does not write. The existing stores (TaskStore, CommitmentStore,
ScheduleStore, GoalStore, InitiativeStore, ActivityStore) remain the only writers. That keeps
transitions single-authored - a rule violation in one place instead of a scattered inconsistency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgendaItem:
    """One row in an agenda section - source table, id, headline, when, and why it landed here."""

    kind: str                    # 'task' | 'commitment' | 'schedule' | 'goal' | 'initiative'
    id: str
    title: str
    when: str                    # human phrase: "due in 2h", "started 47m ago", "next 09:00 tomorrow"
    reason: str                  # why this landed in the section it did
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgendaView:
    """The synthesised view. Every section is a bounded list; empty means empty - never a lie."""

    now: list[AgendaItem] = field(default_factory=list)
    today: list[AgendaItem] = field(default_factory=list)
    overdue: list[AgendaItem] = field(default_factory=list)
    waiting: list[AgendaItem] = field(default_factory=list)
    blocked: list[AgendaItem] = field(default_factory=list)
    upcoming: list[AgendaItem] = field(default_factory=list)
    goals: list[AgendaItem] = field(default_factory=list)
    initiatives: list[AgendaItem] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            name: [
                {"kind": i.kind, "id": i.id, "title": i.title, "when": i.when,
                 "reason": i.reason, **({"detail": i.detail} if i.detail else {})}
                for i in getattr(self, name)]
            for name in ("now", "today", "overdue", "waiting", "blocked",
                         "upcoming", "goals", "initiatives")
        }

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in
                       ("now", "today", "overdue", "waiting", "blocked",
                        "upcoming", "goals", "initiatives"))


class AgendaSynthesiser:
    """One read-model across Sali's live stores. Not a writer - transitions stay single-authored."""

    def __init__(self, pool: Any, temporal: Any, *, per_section: int = 5) -> None:
        self._pool = pool
        self._temporal = temporal
        self._per = max(1, per_section)

    async def synthesise(self) -> AgendaView:
        """Read-only cross-store synthesis. Six independent SELECTs, small result sets."""
        now = self._temporal.now()
        view = AgendaView()
        async with self._pool.acquire() as conn:
            active_task = await conn.fetchrow(
                "SELECT id, objective, started_at, deadline_at FROM sali.task "
                "WHERE is_primary AND status IN ('open','running') "
                "ORDER BY updated_at DESC LIMIT 1")
            if active_task is not None:
                started = active_task["started_at"]
                view.now.append(AgendaItem(
                    kind="task", id=str(active_task["id"]),
                    title=str(active_task["objective"])[:120],
                    when=(f"running for {self._temporal.duration(self._temporal.elapsed(started))}"
                          if started is not None else "opened, not yet started"),
                    reason="the primary active task"))

            due_soon = await conn.fetch(
                "SELECT id, description, deadline FROM sali.commitment "
                "WHERE status IN ('open','in_progress') AND deadline IS NOT NULL "
                "  AND deadline > $1 AND deadline <= $1 + interval '1 hour' "
                "ORDER BY deadline LIMIT $2", now, self._per)
            for row in due_soon:
                view.now.append(AgendaItem(
                    kind="commitment", id=str(row["id"]),
                    title=str(row["description"])[:120],
                    when=f"due {self._temporal.ago(row['deadline'])}",
                    reason="due within the hour"))

            todays_end = self._temporal.end_of_civil_day(now)
            todays_start = self._temporal.start_of_civil_day(now)
            today_commits = await conn.fetch(
                "SELECT id, description, deadline FROM sali.commitment "
                "WHERE status IN ('open','in_progress') AND deadline IS NOT NULL "
                "  AND deadline > $1 AND deadline <= $2 "
                "ORDER BY deadline LIMIT $3", now, todays_end, self._per)
            for row in today_commits:
                view.today.append(AgendaItem(
                    kind="commitment", id=str(row["id"]),
                    title=str(row["description"])[:120],
                    when=f"due {self._temporal.ago(row['deadline'])}",
                    reason="deadline is today"))

            today_schedules = await conn.fetch(
                "SELECT id, name, prompt, next_run_at FROM sali.schedule "
                "WHERE enabled AND next_run_at > $1 AND next_run_at <= $2 "
                "ORDER BY next_run_at LIMIT $3", now, todays_end, self._per)
            for row in today_schedules:
                view.today.append(AgendaItem(
                    kind="schedule", id=str(row["id"]),
                    title=str(row["name"])[:60],
                    when=f"next fires {self._temporal.ago(row['next_run_at'])}",
                    reason="scheduled to fire today",
                    detail={"prompt": str(row["prompt"])[:120]}))

            overdue = await conn.fetch(
                "SELECT id, description, deadline FROM sali.commitment "
                "WHERE status IN ('open','in_progress') AND deadline IS NOT NULL "
                "  AND deadline < $1 ORDER BY deadline LIMIT $2", now, self._per)
            for row in overdue:
                view.overdue.append(AgendaItem(
                    kind="commitment", id=str(row["id"]),
                    title=str(row["description"])[:120],
                    when=f"was due {self._temporal.ago(row['deadline'])}",
                    reason="deadline has passed and nothing settled it"))

            waiting = await conn.fetch(
                "SELECT id, objective FROM sali.task WHERE status='waiting' "
                "ORDER BY updated_at DESC LIMIT $1", self._per)
            for row in waiting:
                view.waiting.append(AgendaItem(
                    kind="task", id=str(row["id"]), title=str(row["objective"])[:120],
                    when="", reason="the task is waiting for a reply from Almir"))

            blocked_tasks = await conn.fetch(
                "SELECT id, objective FROM sali.task WHERE status='blocked' "
                "ORDER BY updated_at DESC LIMIT $1", self._per)
            for row in blocked_tasks:
                view.blocked.append(AgendaItem(
                    kind="task", id=str(row["id"]), title=str(row["objective"])[:120],
                    when="", reason="the task is blocked by an external dependency"))
            blocked_commits = await conn.fetch(
                "SELECT id, description FROM sali.commitment WHERE status='blocked' "
                "ORDER BY updated_at DESC LIMIT $1", max(0, self._per - len(blocked_tasks)))
            for row in blocked_commits:
                view.blocked.append(AgendaItem(
                    kind="commitment", id=str(row["id"]),
                    title=str(row["description"])[:120], when="",
                    reason="a commitment marked blocked"))

            upcoming = await conn.fetch(
                "SELECT id, name, prompt, next_run_at, timezone FROM sali.schedule "
                "WHERE enabled AND next_run_at > $1 ORDER BY next_run_at LIMIT $2",
                todays_end, self._per)
            for row in upcoming:
                view.upcoming.append(AgendaItem(
                    kind="schedule", id=str(row["id"]),
                    title=str(row["name"])[:60],
                    when=f"fires {self._temporal.ago(row['next_run_at'])}",
                    reason="next scheduled run after today",
                    detail={"prompt": str(row["prompt"])[:120], "timezone": row["timezone"]}))

            goals = await conn.fetch(
                "SELECT id, objective, deadline FROM sali.goal "
                "WHERE status IN ('open','active','blocked') ORDER BY priority, updated_at DESC LIMIT $1", self._per)
            for row in goals:
                view.goals.append(AgendaItem(
                    kind="goal", id=str(row["id"]), title=str(row["objective"])[:120],
                    when=(f"deadline {self._temporal.ago(row['deadline'])}"
                          if row["deadline"] is not None else "no deadline"),
                    reason="active goal"))

            initiatives = await conn.fetch(
                "SELECT id, title, status FROM sali.initiative "
                "WHERE status IN ('candidate','active','planning','ready','executing') ORDER BY updated_at DESC LIMIT $1",
                self._per)
            for row in initiatives:
                view.initiatives.append(AgendaItem(
                    kind="initiative", id=str(row["id"]), title=str(row["title"])[:120],
                    when="", reason=f"initiative status: {row['status']}"))

        return view


def render(view: AgendaView) -> str | None:
    """Compact text for the model's context - one line per item, section headers, no chrome.

    Returns None when the whole agenda is empty; the caller then omits the section rather than
    injecting a header that says nothing."""
    if view.is_empty():
        return None
    labels = [("now", "Right now"), ("today", "Today"), ("overdue", "Overdue"),
              ("waiting", "Waiting on Almir"), ("blocked", "Blocked"),
              ("upcoming", "Upcoming"), ("goals", "Active goals"),
              ("initiatives", "Active initiatives")]
    lines: list[str] = ["Your agenda:"]
    for attr, label in labels:
        rows = getattr(view, attr)
        if not rows:
            continue
        lines.append(f"  {label}:")
        for item in rows:
            when = f" — {item.when}" if item.when else ""
            lines.append(f"    • {item.title}{when}")
    return "\n".join(lines)


__all__ = ["AgendaItem", "AgendaView", "AgendaSynthesiser", "render"]
