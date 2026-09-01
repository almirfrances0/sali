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


async def enqueue(conn: Any, *, kind: str, subject: str, reason: str | None = None,
                  priority: int = 5) -> bool:
    """Add a learning item unless the same (kind, subject) is already pending. Returns True if added."""
    row = await conn.fetchrow(
        "INSERT INTO learning_queue (kind, subject, reason, priority) VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (kind, subject) WHERE status='pending' DO NOTHING RETURNING id",
        kind, subject[:280], reason, priority)
    return row is not None


async def pending(conn: Any, *, limit: int = 10) -> list[LearningItem]:
    rows = await conn.fetch(
        "SELECT id, kind, subject, reason, priority FROM learning_queue "
        "WHERE status='pending' ORDER BY priority, created_at LIMIT $1", limit)
    return [LearningItem(id=r["id"], kind=r["kind"], subject=r["subject"], reason=r["reason"],
                         priority=r["priority"]) for r in rows]


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
            "SELECT tool_name, plan->'args'->>'command' AS command, count(*) AS fails "
            "FROM tool_execution "
            "WHERE success IS FALSE "
            "  AND started_at > now() - make_interval(days => $1) "
            "GROUP BY tool_name, plan->'args'->>'command' "
            "HAVING count(*) >= 3 ORDER BY count(*) DESC LIMIT $2",
            budget.scan_window_days, budget.max_new_per_pass)
        for r in failing:
            if added >= budget.max_new_per_pass:
                break
            cmd = r.get("command")
            subject = cmd if cmd and r["tool_name"] == "execute_command" else f"{r['tool_name']} (recurring failure)"
            if await enqueue(conn, kind="recurring_failure", subject=subject,
                             reason=f"{r['tool_name']} failed {r['fails']}× recently", priority=2):
                added += 1
    return added
