"""Runtime coordination state (Cognitive OS §16/§44): the single optional subagent delegation, and the
durable user-clarification question. Both are durable, structured records — the runtime enforces the
0-or-1 subagent invariant and resumes the SAME task when the user answers a question.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.tasks.coordination")


class DelegationStore:
    """At most ONE running delegation per parent task (§15/§16/§17). A unique partial index enforces it
    at the DB level; ``start`` also checks first for a clean error."""

    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def active(self, parent_task_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, objective, status, session_id FROM delegation "
                "WHERE parent_task_id = $1 AND status = 'running' ORDER BY created_at DESC LIMIT 1",
                parent_task_id)
        return dict(row) if row is not None else None

    async def start(
        self, *, parent_task_id: UUID, parent_run_id: UUID | None, objective: str,
        workspace: str | None, session_id: UUID | None,
    ) -> UUID | None:
        """Begin the one delegation. Returns its id, or None if one is already running (0-or-1)."""
        if await self.active(parent_task_id) is not None:
            return None
        did = uuid4()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO delegation (id, parent_task_id, parent_run_id, session_id, objective, "
                    "  workspace, status) VALUES ($1,$2,$3,$4,$5,$6,'running')",
                    did, parent_task_id, parent_run_id, session_id, objective, workspace)
        except Exception as exc:  # noqa: BLE001 - the unique index rejected a concurrent second one
            log.warning("delegation_start_rejected", error=str(exc))
            return None
        await self._emit("delegation.started", parent_task_id, parent_run_id,
                         {"delegation_id": str(did), "objective": objective[:200]})
        return did

    async def complete(self, delegation_id: UUID, *, status: str, result: str | None,
                       parent_task_id: UUID | None = None, parent_run_id: UUID | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE delegation SET status=$1, result=$2, completed_at=now() WHERE id=$3",
                status, result, delegation_id)
        ev = "delegation.completed" if status == "completed" else "delegation.failed"
        await self._emit(ev, parent_task_id, parent_run_id,
                         {"delegation_id": str(delegation_id), "status": status})

    async def list_for_task(self, parent_task_id: UUID) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, objective, status, result, created_at, completed_at FROM delegation "
                "WHERE parent_task_id = $1 ORDER BY created_at", parent_task_id)
        return [dict(r) for r in rows]

    async def _emit(self, event_type: str, task_id: UUID | None, run_id: UUID | None,
                    data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, run_id=run_id, subject_type="task",
                subject_id=task_id, origin="delegation", data=data)


class QuestionStore:
    """A durable user-clarification question (§44). Asking sets the task to 'waiting_for_user'; the user's
    answer resumes the SAME task. A legitimate pause, never a failure — objective/plan/evidence all kept."""

    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def ask(self, task_id: UUID, question: str, *, run_id: UUID | None = None) -> UUID:
        qid = uuid4()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO task_question (id, task_id, run_id, question, status) "
                "VALUES ($1,$2,$3,$4,'pending')", qid, task_id, run_id, question)
            await conn.execute(
                "UPDATE task SET status='waiting_for_user', updated_at=now() "
                "WHERE id=$1 AND status IN ('open','running','waiting','blocked')", task_id)
        await self._emit("task.waiting_for_user", task_id, run_id,
                         {"question_id": str(qid), "question": question[:300]})
        return qid

    async def pending(self, task_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, question FROM task_question WHERE task_id=$1 AND status='pending' "
                "ORDER BY created_at DESC LIMIT 1", task_id)
        return dict(row) if row is not None else None

    async def answer(self, task_id: UUID, answer: str) -> bool:
        """Record the user's answer to the latest pending question and put the task back to 'running'.
        Returns True if a pending question was answered (so the runtime knows to resume the task)."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE task_question SET status='answered', answer=$2, answered_at=now() "
                "WHERE id = (SELECT id FROM task_question WHERE task_id=$1 AND status='pending' "
                "  ORDER BY created_at DESC LIMIT 1) RETURNING id", task_id, answer)
            if row is None:
                return False
            await conn.execute(
                "UPDATE task SET status='running', updated_at=now() "
                "WHERE id=$1 AND status='waiting_for_user'", task_id)
        await self._emit("task.user_answered", task_id, None, {"question_id": str(row["id"])})
        return True

    async def _emit(self, event_type: str, task_id: UUID, run_id: UUID | None,
                    data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, run_id=run_id, subject_type="task",
                subject_id=task_id, origin="clarification", data=data)
