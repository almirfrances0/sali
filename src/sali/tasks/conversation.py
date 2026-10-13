"""ConversationThreadStore + PendingQuestionStore (§3/§4/§7/§16/§23/§24) — human-like async communication.

Waiting is a property of an ACTIVITY, not of Sali as a whole (§1/§2): a blocking question pauses only the
activity that depends on it (via the existing activity ledger); everything else keeps running. A question
has DURABLE identity — thread, person, why it matters, the dependent activity — so it survives compaction
and restart and a late free-text answer can be routed back to the right question days later (§4/§23),
without the model remembering it from its context window. Multiple threads/questions can stay open (§7);
old questions can be superseded rather than resurfaced forever (§24); follow-up is bounded so Sali never
nags (§13/§14/§22). This COMPLEMENTS task_question (the reviewer-gated task-clarification path).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sali.tasks.activity import ActivityStore


class ConversationThreadStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def open_or_create(self, *, person_name: str, topic: str | None = None,
                             person_id: UUID | None = None) -> UUID:
        """Reuse the open thread with this person + topic, or start one. Multiple threads can be open (§7)."""
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM conversation_thread WHERE person_name=$1 "
                "  AND coalesce(topic,'')=coalesce($2,'') AND status IN ('open','paused') "
                "ORDER BY updated_at DESC LIMIT 1", person_name, topic)
            if existing is not None:
                await conn.execute(
                    "UPDATE conversation_thread SET status='open', last_message_at=now(), updated_at=now() "
                    "WHERE id=$1", existing["id"])
                return UUID(str(existing["id"]))
            tid = uuid4()
            await conn.execute(
                "INSERT INTO conversation_thread (id, person_id, person_name, topic, last_message_at) "
                "VALUES ($1,$2,$3,$4,now())", tid, person_id, person_name, topic)
        return tid

    async def pause(self, thread_id: UUID) -> None:
        await self._set_status(thread_id, "paused")

    async def complete(self, thread_id: UUID) -> None:
        await self._set_status(thread_id, "completed")

    async def open_threads(self, *, person_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clause = "status IN ('open','paused')"
        args: list[Any] = []
        if person_name is not None:
            args.append(person_name)
            clause += f" AND person_name=${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, person_name, topic, status, last_message_at FROM conversation_thread "
                f"WHERE {clause} ORDER BY last_message_at DESC NULLS LAST LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM conversation_thread GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"open": by.get("open", 0) + by.get("paused", 0), "by_status": by}

    async def _set_status(self, thread_id: UUID, status: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE conversation_thread SET status=$2, updated_at=now() WHERE id=$1",
                               thread_id, status)


@dataclass(slots=True)
class AnswerRouting:
    """The result of routing a free-text answer to a pending question (§6/§23/§24)."""
    matched: dict[str, Any] | None = None            # the single question the answer belongs to
    ambiguous: list[dict[str, Any]] | None = None    # >1 candidate → ask a natural clarification


class PendingQuestionStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher
        self._activities = ActivityStore(pool, publisher)

    async def ask(
        self, *, question: str, person_name: str, thread_id: UUID | None = None,
        why_it_matters: str | None = None, options: list[str] | None = None,
        dependency_kind: str = "blocking", task_id: UUID | None = None, activity_id: UUID | None = None,
        person_id: UUID | None = None, expires_at: datetime | None = None,
        next_followup: datetime | None = None,
    ) -> UUID:
        """Ask a durable question. A BLOCKING question pauses only its dependent activity (the runtime and
        every other activity keep running, §16); advisory/optional questions pause nothing (§35)."""
        qid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO pending_question (id, thread_id, person_id, person_name, task_id, activity_id, "
                "  question, why_it_matters, options, dependency_kind, expires_at, next_followup) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                qid, thread_id, person_id, person_name, task_id, activity_id, question, why_it_matters,
                options or [], dependency_kind, expires_at, next_followup)
        if dependency_kind == "blocking" and activity_id is not None:
            with contextlib.suppress(Exception):
                await self._activities.wait_for_user(activity_id, question=question)
        await self._emit("conversation.question_created", task_id,
                         {"question_id": str(qid), "person": person_name,
                          "dependency_kind": dependency_kind})
        await self._emit("conversation.waiting", task_id,
                         {"question_id": str(qid), "activity_id": str(activity_id) if activity_id else None})
        return qid

    async def route_answer(
        self, answer_text: str, *, person_name: str | None = None, thread_id: UUID | None = None,
    ) -> AnswerRouting:
        """Route a late free-text answer to the RIGHT pending question (§3/§6/§23). If a thread is
        specified, use its waiting question; else if the person has exactly one waiting question, that's
        it; if several, it's AMBIGUOUS → the caller asks a natural clarification (§6/§24). Does not
        resolve anything on its own — routing then answer() keeps the two steps explicit."""
        waiting = await self.waiting(person_name=person_name, thread_id=thread_id)
        if not waiting:
            return AnswerRouting()
        if len(waiting) == 1:
            return AnswerRouting(matched=waiting[0])
        return AnswerRouting(ambiguous=waiting)

    async def answer(self, question_id: UUID, answer_text: str) -> bool:
        """Record the answer and RESUME only the activity that depended on it — other work is untouched.
        Returns True if a waiting question was answered."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pending_question SET status='answered', answer=$2, answered_at=now() "
                "WHERE id=$1 AND status='waiting' RETURNING task_id, activity_id, dependency_kind",
                question_id, answer_text[:2000])
        if row is None:
            return False
        if row["activity_id"] is not None:
            with contextlib.suppress(Exception):
                await self._activities.resume(UUID(str(row["activity_id"])))
        await self._emit("conversation.answered", row["task_id"], {"question_id": str(question_id)})
        return True

    async def supersede(self, question_id: UUID, *, reason: str) -> None:
        await self._settle(question_id, "superseded", reason=reason)

    async def no_longer_needed(self, question_id: UUID, *, reason: str = "") -> None:
        """Later work made the question irrelevant — retire it instead of resurfacing it forever (§24/§25)."""
        await self._settle(question_id, "no_longer_needed", reason=reason)

    async def cancel(self, question_id: UUID, *, reason: str = "") -> None:
        await self._settle(question_id, "cancelled", reason=reason)

    async def expire_stale(self, *, now: datetime | None = None) -> int:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE pending_question SET status='expired' WHERE status='waiting' "
                "  AND expires_at IS NOT NULL AND expires_at <= coalesce($1, now()) RETURNING id", now)
        return len(rows)

    async def should_follow_up(self, question_id: UUID, *, now: datetime | None = None,
                               max_follow_ups: int = 3) -> bool:
        """Whether it's appropriate to gently resurface a question (§14/§15): still waiting, its follow-up
        time has arrived, and Sali hasn't already nagged past the cap. Silence is not rejection (§14)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, asked_count, next_followup FROM pending_question WHERE id=$1", question_id)
        if row is None or row["status"] != "waiting" or row["asked_count"] >= max_follow_ups:
            if row is not None and row["asked_count"] >= max_follow_ups:
                await self._emit("agent.follow_up_suppressed", None, {"question_id": str(question_id)})
            return False
        if row["next_followup"] is None:
            return True
        return bool(row["next_followup"] <= (now or datetime.now(row["next_followup"].tzinfo)))

    async def record_follow_up(self, question_id: UUID, *, next_followup: datetime | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_question SET asked_count=asked_count+1, last_asked=now(), next_followup=$2 "
                "WHERE id=$1 AND status='waiting'", question_id, next_followup)
        await self._emit("agent.reengagement", None, {"question_id": str(question_id)})

    async def waiting(self, *, person_name: str | None = None, thread_id: UUID | None = None,
                      limit: int = 50) -> list[dict[str, Any]]:
        clause = "status='waiting'"
        args: list[Any] = []
        if thread_id is not None:
            args.append(thread_id)
            clause += f" AND thread_id=${len(args)}"
        if person_name is not None:
            args.append(person_name)
            clause += f" AND person_name=${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, thread_id, person_name, task_id, activity_id, question, why_it_matters, "
                "  options, dependency_kind, asked_count, asked_at FROM pending_question "
                f"WHERE {clause} ORDER BY asked_at LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def get(self, question_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, thread_id, person_name, task_id, activity_id, question, why_it_matters, "
                "  dependency_kind, status, answer, asked_count FROM pending_question WHERE id=$1",
                question_id)
        return dict(row) if row else None

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM pending_question GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"waiting": by.get("waiting", 0), "by_status": by}

    async def _settle(self, question_id: UUID, status: str, *, reason: str) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pending_question SET status=$2, answered_at=now() WHERE id=$1 AND status='waiting' "
                "RETURNING task_id, activity_id", question_id, status)
        if row is not None:
            await self._emit("conversation.superseded", row["task_id"],
                             {"question_id": str(question_id), "status": status, "reason": reason[:160]})
            # a superseded/cancelled question no longer blocks its activity — let it resume
            if row["activity_id"] is not None:
                with contextlib.suppress(Exception):
                    await self._activities.resume(UUID(str(row["activity_id"])))

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id, subject_type="conversation",
                                       origin="runtime", data=data)
