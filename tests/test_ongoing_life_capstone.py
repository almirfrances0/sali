"""Ongoing-life capstone (§39) — Sali waits without stopping living.

Sali works on Project A, hits a decision that needs Almir, and sends a natural question — Project A's
dependent activity waits, but Project B and everything else keep running. Almir doesn't answer; Sali
continues other useful work; context compacts; Sali sends an unrelated proactive message; the process
restarts and every pending question + activity + thread reconstructs from durable state; Almir opens an
unrelated conversation (which does not destroy the pending question) and only LATER answers the original —
Sali routes the free-text answer to the right question, resumes Project A, completes it under the reviewer
gate, and the experience survives cleanup. A previously learned tendency shapes the later interaction.

Stable throughout: task_id (A and B), the pending-question identity, the conversation thread. Waiting
pauses only what depends on the answer — never Sali's whole life.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.behavior import BehaviorStore
from sali.learning.experience import ExperienceStore
from sali.runtime import cognitive
from sali.runtime.behavioral_context import assemble_behavioral_context
from sali.tasks.activity import ActivityStore
from sali.tasks.conversation import ConversationThreadStore, PendingQuestionStore
from sali.tasks.reviewer import ReviewStatus, TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _row(r: Any) -> Any:
    assert r is not None
    return r


def _wired(pool: Any) -> tuple[TaskStore, ExperienceStore]:
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    store._experience_hook = ExperienceStore(pool, pub).extract_and_persist
    return store, ExperienceStore(pool, pub)


async def _complete_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def test_ongoing_life_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    acts = ActivityStore(live_pool, pub)
    threads = ConversationThreadStore(live_pool, pub)
    questions = PendingQuestionStore(live_pool, pub)

    # a previously learned tendency (from earlier evidence) that should shape later interaction
    bs = BehaviorStore(live_pool, pub)
    tend = await bs.propose(trigger="unanswered questions",
                            proposed_behavior="continue independent work while waiting for an answer",
                            scope="global")
    await bs.accept(tend)

    # ── Project A: work → decision needs Almir → natural question (blocks only A's activity) ─────────
    a = await store.create("Project A: build the service", ["design", "build"])
    await store.activate(a.id)
    task_a = a.id
    bound = await store.bind_workspace(a.id, objective="build service", explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])
    a_decide = await acts.start(task_id=a.id, kind="decide", description="choose the datastore")
    db_thread = await threads.open_or_create(person_name="Almir", topic="datastore choice")
    qid = await questions.ask(question="Postgres or MySQL for Project A?", person_name="Almir",
                              thread_id=db_thread, task_id=a.id, activity_id=a_decide,
                              why_it_matters="it decides the migration + driver setup",
                              options=["postgres", "mysql"])

    # ── Project B keeps running — waiting on A does not freeze Sali (§1/§2/§16) ──────────────────────
    b = await store.create("Project B: unrelated cleanup", ["cleanup"])
    b_work = await acts.start(task_id=b.id, kind="cleanup", description="prune old artifacts")
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT status FROM activity WHERE id=$1", a_decide))["status"] \
            == "waiting_for_user"
        assert (await c.fetchrow("SELECT status FROM activity WHERE id=$1", b_work))["status"] == "started"
    await acts.complete(b_work)   # Project B makes real progress while A waits

    # ── COMPACTION: the runtime is alive with a pending question, not frozen (§20) ───────────────────
    snap = (await cognitive.assemble(live_pool, session_id=task_a)).snapshot()
    assert snap["pending_questions"] >= 1 and snap["open_conversations"] >= 1

    # a proactive message for a DIFFERENT reason (§21) — doesn't need a foreground task
    await pub.emit(event_type="agent.message", subject_type="agent_message", origin="background",
                   data={"importance": "normal",
                         "text": "I'm waiting on your datastore decision for Project A, but I finished the "
                                 "Project B cleanup in the meantime."})

    # ── RESTART: brand-new stores reconstruct the pending question + threads + both tasks ────────────
    q2, threads2 = PendingQuestionStore(live_pool, pub), ConversationThreadStore(live_pool)
    got = await q2.get(qid)
    assert got is not None and got["status"] == "waiting" and "migration" in got["why_it_matters"]
    assert (await store.get(task_a)) is not None and (await store.get(b.id)) is not None

    # ── Almir opens an UNRELATED conversation — it must not destroy the pending question (§7) ────────
    await threads2.open_or_create(person_name="Almir", topic="weekend plans")
    assert _row(await q2.get(qid))["status"] == "waiting"

    # ── later, Almir answers the ORIGINAL question — routed to the right one, Project A resumes ──────
    routing = await q2.route_answer("let's go with postgres", person_name="Almir", thread_id=db_thread)
    assert routing.matched is not None and routing.matched["id"] == qid
    assert await q2.answer(qid, "let's go with postgres")
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT status FROM activity WHERE id=$1", a_decide))["status"] == "started"

    # ── Project A completes under the reviewer gate → experience survives cleanup ────────────────────
    await acts.complete(a_decide)
    ex = await store.record_execution(a.id, 1, "scaffold_pg", attempt=1)
    await store.complete_execution(ex, status="completed", result_summary="postgres wired up")
    await _complete_steps(live_pool, a.id)
    assert (await store._reviewer.review(a.id)).status is ReviewStatus.PASSED
    assert await store.finish(a.id, status="done") is None

    # ── assertions (§39) ────────────────────────────────────────────────────────────────────────────
    assert await store.get(task_a) is None                        # Project A completed + archived
    assert not ws.exists()                                        # ephemeral workspace cleaned
    assert any("Project A" in r["content"] for r in await exp.recent())   # experience survives cleanup
    assert _row(await q2.get(qid))["status"] == "answered"            # the original question, resolved
    # future interaction reflects the learned tendency (behavior actually changes future behavior, §17)
    ctx = await assemble_behavioral_context(live_pool, person_name="Almir")
    assert any("independent work while waiting" in t for t in ctx["tendencies"])
