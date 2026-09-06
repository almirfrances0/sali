"""Runtime coordination state (Cognitive OS §44): the durable user-clarification question. Asking
sets the task to 'waiting_for_user'; the user's answer resumes the SAME task.

Brain-audit turn 8 removed `DelegationStore` — Sali is a single executive agent (no subagent
runtime), and the store was inert (the sink never wrote to it). The `delegation` DB table is left
in place (empty), harmless dead schema; dropping it requires a migration + reverse-migration and
is not worth the churn.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.tasks.coordination")


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
