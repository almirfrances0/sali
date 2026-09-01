"""Retrieval-driven historical context (Prompt 6 §13/§14/§29/§44/§45).

Deterministic, indexed queries over the durable execution journal (``task_execution``) so Sali retrieves
only what is relevant to the current step/error/tool — not every historical output — remembers what
FAILED so it never blindly repeats it (negative knowledge), and detects a repeated failing action
instead of looping. The full tool result stays durable in ``tool_execution``; here we surface compact
records plus an ``execution_id`` reference so the full output can be reloaded only when needed.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID


async def recent_failures(pool: Any, task_id: UUID, *, limit: int = 6) -> list[dict[str, Any]]:
    """Negative knowledge (§29): the most recent FAILED executions, so Sali doesn't rediscover a wall."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, step_seq, tool_name, error, attempt, finished_at FROM task_execution "
            "WHERE task_id = $1 AND status = 'failed' ORDER BY started_at DESC LIMIT $2",
            task_id, limit)
    return [dict(r) for r in rows]


async def last_successful_execution(pool: Any, task_id: UUID) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, step_seq, tool_name, result_summary, finished_at FROM task_execution "
            "WHERE task_id = $1 AND status = 'completed' ORDER BY finished_at DESC NULLS LAST LIMIT 1",
            task_id)
    return dict(row) if row is not None else None


def _error_key(error: str | None) -> str | None:
    """A distinctive, stable fragment of an error message for matching a prior identical failure —
    the first quoted/CamelCase/`no such`-style token, else the first few words. Deterministic."""
    if not error:
        return None
    m = re.search(r"([A-Z][A-Za-z]*Error|[Nn]o such \w+|not found|cannot \w+|E[A-Z]{3,})", error)
    if m:
        return m.group(1)
    return " ".join(error.split()[:4]) or None


async def relevant_executions(
    pool: Any, task_id: UUID, *, error: str | None = None, tool: str | None = None,
    step: int | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    """Executions relevant to the CURRENT situation (§14/§44): the same tool, the same step, or a prior
    execution that hit the same error — deterministic (indexed match + recency), not the whole journal."""
    clauses: list[str] = []
    args: list[Any] = [task_id]
    if tool:
        args.append(tool)
        clauses.append(f"tool_name = ${len(args)}")
    if step is not None:
        args.append(step)
        clauses.append(f"step_seq = ${len(args)}")
    key = _error_key(error)
    if key:
        args.append(f"%{key}%")
        clauses.append(f"error ILIKE ${len(args)}")
    if not clauses:
        return []
    args.append(limit)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, step_seq, tool_name, status, result_summary, error, finished_at "
            f"FROM task_execution WHERE task_id = $1 AND ({' OR '.join(clauses)}) "
            f"ORDER BY started_at DESC LIMIT ${len(args)}", *args)
    return [dict(r) for r in rows]


async def repeated_failures(
    pool: Any, task_id: UUID, *, threshold: int = 3,
) -> list[dict[str, Any]]:
    """Deterministic repeated-failed-action detection (§45): a (tool, error) that has failed at least
    ``threshold`` times — a signal to research / try an alternative / ask, rather than loop."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tool_name, error, count(*) AS n FROM task_execution "
            "WHERE task_id = $1 AND status = 'failed' AND error IS NOT NULL "
            "GROUP BY tool_name, error HAVING count(*) >= $2 ORDER BY count(*) DESC",
            task_id, threshold)
    return [dict(r) for r in rows]
