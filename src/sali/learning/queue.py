"""The learning queue + curiosity (spec §45/§46/§79).

Sali's long-term learning agenda: a durable list of things it noticed it doesn't understand and should
investigate when it has the budget — an unknown service that appeared, a procedure that keeps failing.
Curiosity is deliberately BOUNDED (§46/§79): the background pass enqueues at most a few new items per
run, so noticing gaps never turns into infinite autonomous experimentation. Populated deterministically
from signals that already exist (attention's `investigate` observations, recurring tool failures) — no
model needed to notice a gap; interpreting it is the model's job later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class Budget:
    """Bounds curiosity so it stays practical on a local machine (§79)."""
    max_new_per_pass: int = 3   # new items the background pass may enqueue in one run
    scan_window_days: int = 1   # how far back to look for fresh gaps


@dataclass(slots=True)
class LearningItem:
    id: UUID
    kind: str
    subject: str
    reason: str | None
    priority: int
    # What this gap BLOCKED, when it blocked something. Learning that is not attached to the work it
    # unblocks can never come back and finish it — the whole point of failure→learn→retry (§18).
    task_id: UUID | None = None
    step_seq: int | None = None


async def enqueue(conn: Any, *, kind: str, subject: str, reason: str | None = None,
                  priority: int = 5, task_id: Any = None, step_seq: int | None = None) -> bool:
    """Add a learning item unless the same (kind, subject) is already pending. Returns True if added."""
    row = await conn.fetchrow(
        "INSERT INTO learning_queue (kind, subject, reason, priority, task_id, step_seq) "
        "VALUES ($1,$2,$3,$4,$5,$6) "
        "ON CONFLICT (kind, subject) WHERE status='pending' DO NOTHING RETURNING id",
        kind, subject[:280], reason, priority, task_id, step_seq)
    if row is None and task_id is not None:
        # The gap was already queued — from an earlier pass, or before it blocked anything. Attach the
        # work now rather than dropping the link: the answer is coming either way, and without this the
        # first sighting of a subject would permanently decide whether it can ever unblock a step.
        await conn.execute(
            "UPDATE learning_queue SET task_id=$3, step_seq=$4 "
            "WHERE kind=$1 AND subject=$2 AND status='pending' AND task_id IS NULL",
            kind, subject[:280], task_id, step_seq)
    return row is not None


async def pending(conn: Any, *, limit: int = 10) -> list[LearningItem]:
    rows = await conn.fetch(
        "SELECT id, kind, subject, reason, priority, task_id, step_seq FROM learning_queue "
        # A gap that is holding up real work is researched before idle curiosity, whatever its priority.
        "WHERE status='pending' ORDER BY (task_id IS NULL), priority, created_at LIMIT $1", limit)
    return [LearningItem(id=r["id"], kind=r["kind"], subject=r["subject"], reason=r["reason"],
                         priority=r["priority"], task_id=r["task_id"],
                         step_seq=r["step_seq"]) for r in rows]


async def count_pending(conn: Any) -> int:
    return int(await conn.fetchval("SELECT count(*) FROM learning_queue WHERE status='pending'") or 0)


async def resolve(conn: Any, item_id: UUID, *, outcome: str) -> None:
    await conn.execute(
        "UPDATE learning_queue SET status='resolved', outcome=$2, resolved_at=now() WHERE id=$1",
        item_id, outcome[:280])


async def drop(conn: Any, item_id: UUID) -> None:
    await conn.execute(
        "UPDATE learning_queue SET status='dropped', resolved_at=now() WHERE id=$1", item_id)


async def queue_gaps(conn: Any, *, budget: Budget | None = None) -> int:
    """Notice new learning gaps and enqueue them, up to the budget. Deterministic; caller owns the txn.
    Sources: attention's `investigate` observations, and commands that keep failing. Returns #enqueued."""
    budget = budget or Budget()
    added = 0

    # 1) Attention already flagged these as worth interpreting (§14 investigate).
    investigate = await conn.fetch(
        "SELECT payload->>'summary' AS summary, max(created_at) AS latest FROM event "
        "WHERE event_type='desktop.observed' AND payload->>'action'='investigate' "
        "  AND created_at > now() - make_interval(days => $1) "
        "GROUP BY payload->>'summary' ORDER BY latest DESC LIMIT $2",
        budget.scan_window_days, budget.max_new_per_pass * 2)
    for r in investigate:
        if added >= budget.max_new_per_pass:
            break
        subject = (r["summary"] or "").strip()
        if subject and await enqueue(conn, kind="investigate", subject=subject,
                                     reason="attention flagged this to look into", priority=3):
            added += 1

    # 2) A tool that has failed repeatedly is a knowledge gap worth understanding (§58).
    # Covers ALL tools. For execute_command, includes the specific command in the subject.
    if added < budget.max_new_per_pass:
        failing = await conn.fetch(
            # The most RECENT failure's task/step comes along, so researching this gap can hand the
            # answer back to the work that is waiting on it.
            "SELECT te.tool_name, te.plan->'args'->>'command' AS command, count(*) AS fails, "
            "  (array_agg(tx.task_id ORDER BY te.started_at DESC) "
            "     FILTER (WHERE tx.task_id IS NOT NULL))[1] AS task_id, "
            "  (array_agg(tx.step_seq ORDER BY te.started_at DESC) "
            "     FILTER (WHERE tx.step_seq IS NOT NULL))[1] AS step_seq "
            "FROM tool_execution te "
            "LEFT JOIN task_execution tx ON tx.execution_id = te.id "
            "WHERE te.success IS FALSE "
            "  AND te.started_at > now() - make_interval(days => $1) "
            "GROUP BY te.tool_name, te.plan->'args'->>'command' "
            "HAVING count(*) >= 3 ORDER BY count(*) DESC LIMIT $2",
            budget.scan_window_days, budget.max_new_per_pass)
        for r in failing:
            if added >= budget.max_new_per_pass:
                break
            cmd = r.get("command")
            subject = cmd if cmd and r["tool_name"] == "execute_command" else f"{r['tool_name']} (recurring failure)"
            if await enqueue(conn, kind="recurring_failure", subject=subject,
                             reason=f"{r['tool_name']} failed {r['fails']}× recently", priority=2,
                             task_id=r["task_id"], step_seq=r["step_seq"]):
                added += 1

    # 3) A STEP THAT IS ACTUALLY STUCK. The two sources above are statistical — they need a pattern
    # (3+ failures) or attention's interest before Sali notices anything. But a single failed step on a
    # live task is the case that matters most: work is stopped RIGHT NOW and one answer restarts it.
    # Priority 1 (ahead of everything) and a wider window, because a blocked task can sit for days.
    if added < budget.max_new_per_pass:
        stuck = await conn.fetch(
            "SELECT s.task_id, s.seq, s.description, s.last_error, s.failure_class "
            "FROM task_step s JOIN task t ON t.id = s.task_id "
            "WHERE s.status = 'failed' AND s.last_error IS NOT NULL "
            "  AND t.status NOT IN ('done','failed','abandoned','cancelled','superseded') AND t.archived_at IS NULL "
            # 'completed' was never a status this schema has — the terminal words are done/failed/
            # abandoned/cancelled/superseded — so this guard used to admit finished AND ABANDONED
            # work. Queueing research for a step on a task Almir stopped is the first half of
            # resurrecting dead work.
            "  AND NOT EXISTS (SELECT 1 FROM revoked_intent r "
            "                  WHERE r.task_id = t.id AND r.superseded_by IS NULL) "
            "  AND s.finished_at > now() - interval '7 days' "
            # Nothing already queued for this exact step.
            "  AND NOT EXISTS (SELECT 1 FROM learning_queue q WHERE q.status='pending' "
            "                    AND q.task_id = s.task_id AND q.step_seq = s.seq) "
            "ORDER BY s.finished_at DESC LIMIT $1",
            budget.max_new_per_pass)
        for r in stuck:
            if added >= budget.max_new_per_pass:
                break
            # The subject is what gets searched, so lead with the error — that is the thing to solve —
            # and keep the step for context. `blocked_step` is a distinct kind so it can never collide
            # with the recurring-failure entry for the same tool.
            err = " ".join((r["last_error"] or "").split())[:180]
            desc = " ".join((r["description"] or "").split())[:90]
            subject = f"{err} (while: {desc})" if desc else err
            if await enqueue(conn, kind="blocked_step", subject=subject,
                             reason=f"step {r['seq']} is stuck on this"
                                    + (f" ({r['failure_class']})" if r["failure_class"] else ""),
                             priority=1, task_id=r["task_id"], step_seq=r["seq"]):
                added += 1
    return added
