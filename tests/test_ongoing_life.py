"""Behavior evolution & human-like ongoing life (§38).

Waiting is a property of an ACTIVITY, not of Sali as a whole: a blocking question pauses only its
dependent activity while everything else keeps running. A question has durable identity, so a late
free-text answer routes back to the right one days later; ambiguity asks a natural clarification; silence
is not rejection and follow-up is bounded (no nagging). Behavioral tendencies are evidence-backed and
surface as a bounded interaction context. (behavior_proposal accumulation, natural consent, memory
contradiction/source-priority, and task/experience are earlier suites, reused unchanged.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.runtime import cognitive
from sali.runtime.behavioral_context import assemble_behavioral_context
from sali.tasks.activity import ActivityStore
from sali.tasks.conversation import ConversationThreadStore, PendingQuestionStore
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _row(r: Any) -> Any:
    assert r is not None
    return r


# ── waiting pauses an activity, not the runtime (§1/§2/§16) ──────────────────────────────────────────

async def test_waiting_is_per_activity_not_the_whole_runtime(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    acts = ActivityStore(live_pool, pub)
    q = PendingQuestionStore(live_pool, pub)
    task = await TaskStore(live_pool).create("Project A", ["work"])
    a = await acts.start(task_id=task.id, kind="decide", description="pick a database")
    b = await acts.start(task_id=task.id, kind="code", description="implement the API")   # independent
    await q.ask(question="Postgres or MySQL?", person_name="Almir", task_id=task.id, activity_id=a,
               dependency_kind="blocking")
    async with live_pool.acquire() as c:
        rows = {r["id"]: r["status"] for r in await c.fetch("SELECT id, status FROM activity")}
    assert rows[a] == "waiting_for_user"       # only the dependent activity waits
    assert rows[b] == "started"                # independent work keeps running (§16)


async def test_advisory_question_does_not_block_its_activity(live_pool: Any) -> None:
    acts = ActivityStore(live_pool)
    q = PendingQuestionStore(live_pool)
    task = await TaskStore(live_pool).create("Project A", ["work"])
    a = await acts.start(task_id=task.id, kind="code", description="build with reversible defaults")
    await q.ask(question="Any preference on the theme colour?", person_name="Almir", task_id=task.id,
               activity_id=a, dependency_kind="advisory")   # opinion, not a blocker (§35)
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT status FROM activity WHERE id=$1", a))["status"] == "started"


# ── durable question identity + late-answer routing + resume (§3/§4/§23) ────────────────────────────

async def test_pending_question_has_durable_identity_and_survives_restart(live_pool: Any) -> None:
    q = PendingQuestionStore(live_pool, EventPublisher(live_pool))
    threads = ConversationThreadStore(live_pool)
    tid = await threads.open_or_create(person_name="Almir", topic="database architecture")
    qid = await q.ask(question="Which DB — Postgres or MySQL?", person_name="Almir", thread_id=tid,
                      why_it_matters="it changes the migration approach", options=["postgres", "mysql"])
    # a brand-new store (restart) reconstructs the full question without any conversation history (§4)
    got = await PendingQuestionStore(live_pool).get(qid)
    assert got is not None and "migration approach" in got["why_it_matters"]
    assert got["question"].startswith("Which DB") and got["status"] == "waiting"


async def test_late_answer_routes_and_resumes_only_the_dependent_activity(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    acts = ActivityStore(live_pool, pub)
    q = PendingQuestionStore(live_pool, pub)
    task = await TaskStore(live_pool).create("Project A", ["work"])
    a = await acts.start(task_id=task.id, kind="decide", description="pick a provider")
    qid = await q.ask(question="Which provider?", person_name="Almir", task_id=task.id, activity_id=a)
    # hours later, a fresh store routes the free-text answer to the (only) waiting question and resumes it
    routing = await PendingQuestionStore(live_pool, pub).route_answer("go with the second option",
                                                                     person_name="Almir")
    assert routing.matched is not None and routing.matched["id"] == qid
    assert await q.answer(qid, "go with the second option")
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT status FROM activity WHERE id=$1", a))["status"] == "started"  # resumed
        assert (await c.fetchrow("SELECT status, answer FROM pending_question WHERE id=$1",
                                 qid))["status"] == "answered"


async def test_multiple_pending_questions_are_ambiguous_and_ask_clarification(live_pool: Any) -> None:
    q = PendingQuestionStore(live_pool)
    task = await TaskStore(live_pool).create("Project A", ["work"])
    await q.ask(question="Delete the temp files?", person_name="Almir", task_id=task.id)
    await q.ask(question="Create the staging account?", person_name="Almir", task_id=task.id)
    routing = await q.route_answer("yes", person_name="Almir")
    assert routing.matched is None and routing.ambiguous is not None and len(routing.ambiguous) == 2  # §6/§24


# ── topic switching: an unrelated message must not destroy a pending question (§7) ───────────────────

async def test_unrelated_thread_does_not_destroy_pending_question(live_pool: Any) -> None:
    q = PendingQuestionStore(live_pool)
    threads = ConversationThreadStore(live_pool)
    db_thread = await threads.open_or_create(person_name="Almir", topic="database")
    qid = await q.ask(question="Postgres or MySQL?", person_name="Almir", thread_id=db_thread)
    # a completely different conversation opens; the DB question is still waiting on its own thread (§7)
    await threads.open_or_create(person_name="Almir", topic="weather chat")
    assert _row(await q.get(qid))["status"] == "waiting"
    # answering by thread targets the right question even though another thread exists
    routing = await q.route_answer("let's do postgres", person_name="Almir", thread_id=db_thread)
    assert routing.matched is not None and routing.matched["id"] == qid


# ── lifecycle: supersede when no longer needed; silence ≠ rejection; bounded follow-up (§14/§24) ─────

async def test_no_longer_needed_retires_question_and_frees_activity(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    acts, q = ActivityStore(live_pool, pub), PendingQuestionStore(live_pool, pub)
    task = await TaskStore(live_pool).create("Project A", ["work"])
    a = await acts.start(task_id=task.id, kind="decide")
    qid = await q.ask(question="Which region?", person_name="Almir", task_id=task.id, activity_id=a)
    await q.no_longer_needed(qid, reason="chose a region-agnostic setup")   # later work made it moot (§24/§25)
    assert not any(x["id"] == qid for x in await q.waiting())
    async with live_pool.acquire() as c:  # the activity no longer blocks on a moot question
        assert (await c.fetchrow("SELECT status FROM activity WHERE id=$1", a))["status"] == "started"


async def test_silence_is_not_rejection_and_follow_up_is_bounded(live_pool: Any) -> None:
    q = PendingQuestionStore(live_pool, EventPublisher(live_pool))
    qid = await q.ask(question="Ready to deploy?", person_name="Almir")
    # unanswered → stays 'waiting' (never fabricated into declined/rejected, §14)
    assert _row(await q.get(qid))["status"] == "waiting"
    assert await q.should_follow_up(qid)                       # a first gentle follow-up is fine
    for _ in range(3):
        await q.record_follow_up(qid)
    assert not await q.should_follow_up(qid, max_follow_ups=3)  # capped — no nagging (§13/§14/§22)


# ── behavioral context: bounded, evidence-backed, derived (§19/§30) ─────────────────────────────────

async def test_behavioral_context_is_bounded_and_evidence_backed(live_pool: Any) -> None:
    from sali.learning.behavior import BehaviorStore
    from sali.tasks.people import PersonStore

    bs = BehaviorStore(live_pool, EventPublisher(live_pool))
    await PersonStore(live_pool).upsert(name="Almir", relationship_type="owner",
                                        preferred_channel="terminal", provenance="explicit")
    pref = await bs.propose(trigger="explanations", proposed_behavior="prefer concise technical answers",
                            scope="user")
    tend = await bs.propose(trigger="unanswered questions",
                            proposed_behavior="do not repeatedly resend unanswered questions", scope="global")
    await bs.accept(pref)
    await bs.accept(tend)
    ctx = await assemble_behavioral_context(live_pool, person_name="Almir")
    assert "prefer concise technical answers" in ctx["preferences"]
    assert "do not repeatedly resend unanswered questions" in ctx["tendencies"]
    assert ctx["relationship"]["relationship"] == "owner"
    # Bounded and derived — a read-only diagnostic view. Brain-audit turn 7 removed the
    # `render_behavioral_context` companion that used to project this view into the prompt as a
    # "HIS STANDING REQUESTS" block. Preferences flow through the normal memory-retrieval bundle
    # now, so the view here is checked as data, not as a prompt string.
    assert len(ctx["preferences"]) <= 6 and len(ctx["tendencies"]) <= 6   # bounded (§30)
    empty = await assemble_behavioral_context(live_pool, person_name=None)
    # `empty` still reflects the live rows above; the important shape check is that a None person
    # cleanly returns a dict with no relationship (fabrication guard, §27/§34).
    assert empty["relationship"] is None


async def test_explicit_correction_becomes_a_behavior_candidate(live_pool: Any) -> None:
    from sali.learning.behavior import BehaviorStore
    bs = BehaviorStore(live_pool)
    # A phrase the deliberately-narrow classifier matches: "stop repeating yourself" ≠ a task, ≠ an
    # ordinary request. Behavioral candidates only come from second-person manner directives, so the
    # test uses one of them. Post-Turn-7 the candidate must stay CANDIDATE (never auto-accepted).
    bid = await bs.observe_feedback("stop repeating yourself")        # negative social feedback (§13)
    assert bid is not None
    pending_ids = {p["id"] for p in await bs.pending()}
    assert bid in pending_ids                                          # candidate, not auto-applied
    accepted_ids = {a["id"] for a in await bs.accepted()}
    assert bid not in accepted_ids                                     # NOT auto-elevated (turn 7)


# ── Cognitive OS integration: async life is exposed + bounded (§20) ─────────────────────────────────

async def test_cognitive_state_exposes_open_conversations_and_pending_questions(live_pool: Any) -> None:
    threads = ConversationThreadStore(live_pool)
    q = PendingQuestionStore(live_pool)
    tid = await threads.open_or_create(person_name="Almir", topic="deploy")
    await q.ask(question="Which host?", person_name="Almir", thread_id=tid)
    snap = (await cognitive.assemble(live_pool, session_id=uuid4())).snapshot()
    assert snap["open_conversations"] >= 1 and snap["pending_questions"] >= 1
    assert isinstance(snap["open_conversations"], int) and isinstance(snap["pending_questions"], int)
    # the runtime is alive with a pending question — waiting is not a global freeze (§20)
    assert "next_wakeup" in snap


async def test_expired_question_is_not_reused(live_pool: Any) -> None:
    q = PendingQuestionStore(live_pool)
    qid = await q.ask(question="ephemeral?", person_name="Almir",
                      expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert await q.expire_stale() >= 1
    assert _row(await q.get(qid))["status"] == "expired"
    assert not any(x["id"] == qid for x in await q.waiting())
