"""Adaptive intelligence, daily learning & behavior evolution (Prompt 7 §38).

The whole point of this layer is that Sali gets smarter WITHOUT the model becoming the source of truth:
confidence is derived from evidence, a lone assertion or failure never becomes knowledge, duplicates
merge, contradictions are recorded not overwritten, scope is preserved, and daily consolidation is
idempotent + bounded. These tests pin every one of those rules.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.behavior import BehaviorStore, classify_feedback
from sali.learning.candidates import LearningCandidateStore
from sali.learning.daily import DailyConsolidation
from sali.learning.evidence import EvidenceLevel, derive_confidence, is_promotable
from sali.learning.retrieval import relevant_knowledge, render_hints
from sali.learning.skill_evolution import SkillProposalStore

pytestmark = pytest.mark.db


async def _get(pool: Any, cid: Any) -> Any:
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM learning_candidate WHERE id=$1", cid)


async def _force_state(pool: Any, cid: Any, state: str, *, level: int = 5) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE learning_candidate SET verification_state=$2, evidence_level=$3 WHERE id=$1",
            cid, state, level)


# ── the evidence hierarchy (pure, no DB) — §3/§4/§5 ─────────────────────────────────────────────────

def test_confidence_is_derived_from_evidence_never_invented() -> None:
    # a stronger evidence level yields higher base confidence; outcomes move it; bounded [0.05, 0.99]
    assert derive_confidence(evidence_level=0) < derive_confidence(evidence_level=int(EvidenceLevel.REVIEWER_VERIFIED))
    up = derive_confidence(evidence_level=3, times_successful=5, times_failed=0)
    down = derive_confidence(evidence_level=3, times_successful=0, times_failed=5)
    assert 0.05 <= down < up <= 0.99


def test_promotion_gate_never_promotes_a_lone_assertion_or_a_single_failure() -> None:
    # a bare model assertion (level 0) is never promotable
    assert not is_promotable(evidence_level=0, verification_state="observed")
    # a reviewer PASS (level 5) is
    assert is_promotable(evidence_level=5, verification_state="verified")
    # repeated success clears the bar; a single success does not
    assert is_promotable(evidence_level=3, verification_state="verified", times_successful=3, times_failed=0)
    assert not is_promotable(evidence_level=3, verification_state="verified", times_successful=1)
    # a contradicted / rejected candidate is never promotable, whatever the level
    assert not is_promotable(evidence_level=5, verification_state="contradicted")
    # a NEGATIVE lesson needs repeated failure AND a verified explanation (§5)
    assert not is_promotable(evidence_level=3, verification_state="verified", source_type="failure",
                             times_failed=1, has_explanation=True)
    assert not is_promotable(evidence_level=3, verification_state="verified", source_type="failure",
                             times_failed=3, has_explanation=False)
    assert is_promotable(evidence_level=3, verification_state="verified", source_type="failure",
                         times_failed=3, has_explanation=True)


# ── learning candidates: durability, evidence, dedup, contradiction, scope (§38) ────────────────────

async def test_candidate_is_durable_and_survives_restart(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool, EventPublisher(live_pool))
    cid = await lc.observe(lesson="Laravel Breeze provides auth scaffolding", scope="skill",
                           scope_ref="laravel", evidence_level=int(EvidenceLevel.EXTERNAL_OBSERVATION))
    # a brand-new store (simulated process restart) reconstructs it from PostgreSQL alone
    assert any(r["id"] == cid for r in await LearningCandidateStore(live_pool).recent())


async def test_failed_observation_never_becomes_verified_knowledge(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    cid = await lc.observe(lesson="command A always works", scope="task", evidence_level=0)
    await lc.record_outcome(cid, success=False)
    row = await _get(live_pool, cid)
    assert row["verification_state"] == "attempted" and row["times_failed"] == 1
    assert not await lc.promotable()  # a failed experiment is never promotable (§5)


async def test_verified_execution_and_repeated_success_strengthen_evidence(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    cid = await lc.observe(lesson="composer install works on this box", scope="environment",
                           scope_ref="kali", evidence_level=int(EvidenceLevel.EXTERNAL_OBSERVATION))
    c0 = (await _get(live_pool, cid))["confidence"]
    await lc.record_outcome(cid, success=True)   # one real success → TOOL_SUCCESS
    await lc.record_outcome(cid, success=True)   # repeated → REPEATED_SUCCESS + 'supported'
    row = await _get(live_pool, cid)
    assert row["evidence_level"] >= int(EvidenceLevel.REPEATED_SUCCESS)
    assert row["verification_state"] == "supported" and row["confidence"] > c0


async def test_duplicate_observations_merge_not_multiply(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    c1 = await lc.observe(lesson="use PostgreSQL for prod", scope="project", scope_ref="/p", evidence_level=1)
    c2 = await lc.observe(lesson="Use   PostgreSQL for   prod", scope="project", scope_ref="/p", evidence_level=2)
    assert c1 == c2  # same lesson in the same scope → one candidate (§13)
    row = await _get(live_pool, c1)
    assert row["times_observed"] == 2 and row["evidence_level"] == 2


async def test_contradiction_is_recorded_and_resolved_by_evidence_priority(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool, EventPublisher(live_pool))
    old = await lc.observe(lesson="Laravel 10 requires config A", scope="skill", scope_ref="laravel",
                           evidence_level=2, claim_key="laravel10_config_A", claim_value="required")
    new = await lc.observe(lesson="Laravel 10 no longer requires config A", scope="skill",
                           scope_ref="laravel", evidence_level=5,
                           claim_key="laravel10_config_A", claim_value="not_required")
    cx = await lc.contradictions()
    assert cx and cx[0]["claim_key"] == "laravel10_config_A"
    # the STRONGER new evidence wins; the old claim is marked contradicted — recorded, not overwritten
    assert (await _get(live_pool, old))["verification_state"] == "contradicted"
    assert (await _get(live_pool, new))["verification_state"] != "contradicted"
    assert cx[0]["status"] == "resolved" and old != new


async def test_scope_is_preserved_and_task_knowledge_is_not_global(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    cid = await lc.observe(lesson="this project uses feature flags", scope="project", scope_ref="/proj",
                           evidence_level=3)
    await _force_state(live_pool, cid, "verified")
    assert (await _get(live_pool, cid))["scope"] == "project"
    assert not await lc.active(scope="global")   # nothing silently became global (§8)
    assert await lc.active(scope="project")


async def test_superseded_knowledge_is_not_preferred(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    old = await lc.observe(lesson="old approach to X", scope="task", evidence_level=3)
    new = await lc.observe(lesson="new approach to X", scope="task", evidence_level=3)
    await _force_state(live_pool, old, "verified")
    await _force_state(live_pool, new, "verified")
    await lc.supersede(old, new)
    ids = [r["id"] for r in await lc.active(scope="task")]
    assert old not in ids and new in ids   # superseded is excluded from the preferred/active set


# ── behavioral learning: feedback → candidate, scope, acceptance authority (§6/§7/§16/§42) ───────────

def test_user_feedback_becomes_a_structured_observation() -> None:
    def _s(msg: str) -> str:
        obs = classify_feedback(msg)
        assert obs is not None
        return obs.sentiment
    assert _s("Always use PostgreSQL") == "directive"
    assert _s("don't do that again") == "prohibition"
    assert _s("next time ask me before deploying") == "prohibition"
    assert _s("I prefer tabs over spaces") == "preference"
    assert _s("that was wrong") == "correction"
    assert classify_feedback("just a normal question about the weather") is None  # most turns → nothing


async def test_user_preference_becomes_a_candidate_not_auto_accepted(live_pool: Any) -> None:
    bs = BehaviorStore(live_pool, EventPublisher(live_pool))
    bid = await bs.observe_feedback("always use PostgreSQL")
    assert bid is not None
    assert any(p["id"] == bid for p in await bs.pending())   # a candidate, pending
    assert not await bs.accepted()                           # NOT auto-accepted — needs authority (§42)


async def test_repeated_feedback_reinforces_without_duplicating(live_pool: Any) -> None:
    bs = BehaviorStore(live_pool)
    b1 = await bs.observe_feedback("always use PostgreSQL")
    b2 = await bs.observe_feedback("always use PostgreSQL")
    assert b1 == b2   # merged (§13)
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT times_observed, confidence FROM behavior_proposal WHERE id=$1", b1)
    assert row["times_observed"] == 2 and row["confidence"] > 0.5


async def test_project_preference_does_not_become_global_and_accept_affects_profile(live_pool: Any) -> None:
    bs = BehaviorStore(live_pool, EventPublisher(live_pool))
    proj = await bs.propose(trigger="in this project", proposed_behavior="prefer existing conventions",
                            scope="project", scope_ref="/p")
    await bs.accept(proj)
    prof = await bs.profile()
    assert "prefer existing conventions" in " ".join(prof.project_preferences)
    assert "prefer existing conventions" not in " ".join(prof.known_user_preferences)  # scope preserved


async def test_rejected_proposal_does_not_affect_behavior(live_pool: Any) -> None:
    bs = BehaviorStore(live_pool)
    accepted = await bs.propose(trigger="t", proposed_behavior="ask before deploying", scope="user")
    rejected = await bs.propose(trigger="t2", proposed_behavior="delete without asking", scope="user")
    await bs.accept(accepted)
    await bs.reject(rejected)
    joined = " ".join((await bs.profile()).known_user_preferences)
    assert "ask before deploying" in joined and "delete without asking" not in joined


# ── daily consolidation: idempotent, restart-safe, bounded catch-up, no foreground lease (§9/§26/§27) ─

async def test_daily_consolidation_is_idempotent_and_restart_safe(live_pool: Any) -> None:
    day = date(2026, 3, 1)
    s1 = await DailyConsolidation(live_pool, publisher=EventPublisher(live_pool)).run(on_date=day)
    assert not s1.skipped
    assert (await DailyConsolidation(live_pool).run(on_date=day)).skipped  # same day again → no-op
    # a brand-new object (process restart) still refuses to re-run the day (the ledger is durable)
    assert (await DailyConsolidation(live_pool).run(on_date=day)).skipped


async def test_missed_days_do_bounded_catch_up(live_pool: Any) -> None:
    from datetime import UTC, datetime
    today = datetime.now(UTC).date()
    async with live_pool.acquire() as c:  # a completed cycle 6 days ago; 5 days are "missed"
        await c.execute("INSERT INTO consolidation_run (ran_on, status, completed_at) "
                        "VALUES ($1,'completed',now()) ON CONFLICT DO NOTHING", today - timedelta(days=6))
    summaries = await DailyConsolidation(live_pool).catch_up(max_cycles=2)
    assert len(summaries) <= 2   # bounded — never runs all five missed days (§26)


async def test_daily_promotes_verified_candidate_into_durable_memory(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool, EventPublisher(live_pool))
    cid = await lc.observe(lesson="Laravel Breeze installs auth via composer require laravel/breeze",
                           scope="skill", scope_ref="laravel", evidence_level=2)
    await _force_state(live_pool, cid, "verified", level=int(EvidenceLevel.REVIEWER_VERIFIED))
    summary = await DailyConsolidation(live_pool, publisher=EventPublisher(live_pool)).run(on_date=date(2026, 3, 2))
    assert summary.promoted >= 1
    assert (await _get(live_pool, cid))["promoted"] is True
    async with live_pool.acquire() as c:  # a durable, reusable memory now exists for the lesson
        assert await c.fetchval(
            "SELECT 1 FROM memory WHERE claim_key=$1 AND valid_until IS NULL", f"lesson:{cid}")


async def test_daily_learning_never_takes_the_foreground_lease(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        before = await c.fetchval("SELECT status FROM execution_lease WHERE id='foreground'")
    await DailyConsolidation(live_pool).run(on_date=date(2026, 3, 3))
    async with live_pool.acquire() as c:
        after = await c.fetchval("SELECT status FROM execution_lease WHERE id='foreground'")
    assert before == after   # background learning never grabs the single foreground execution (§27/§37)


# ── skill evolution: proposals + reproducible versions (§14/§15) ─────────────────────────────────────

async def test_skill_improvement_creates_a_reviewable_proposal(live_pool: Any) -> None:
    sp = SkillProposalStore(live_pool, EventPublisher(live_pool))
    pid = await sp.propose(skill_name="laravel", current_guidance="use Jetstream",
                           proposed_change="prefer Breeze for simple auth", reason="repeated success",
                           times_successful=3)
    pend = await sp.pending()
    assert any(p["id"] == pid for p in pend)   # a proposal, not an applied change (§14)


async def test_existing_skill_versions_remain_reproducible(live_pool: Any) -> None:
    # two tasks snapshot the SAME skill at two different contents → two durable, reproducible versions
    from sali.tasks.store import TaskStore
    store = TaskStore(live_pool)
    t1 = await store.create("t1", ["a"])
    t2 = await store.create("t2", ["a"])
    async with live_pool.acquire() as c:
        await c.execute("INSERT INTO task_skill (task_id, name, path, content_hash, content) "
                        "VALUES ($1,'laravel','/s.md','hashV1','version 1 guidance')", t1.id)
        await c.execute("INSERT INTO task_skill (task_id, name, path, content_hash, content) "
                        "VALUES ($1,'laravel','/s.md','hashV7','version 7 guidance')", t2.id)
    versions = await SkillProposalStore(live_pool).versions("laravel")
    hashes = {v["content_hash"] for v in versions}
    assert {"hashV1", "hashV7"} <= hashes   # each task keeps the exact version it started on (§15)


# ── retrieval: relevant in, irrelevant out, contradicted excluded, bounded (§20/§33/§34/§35) ─────────

async def test_relevant_knowledge_is_retrieved_and_irrelevant_excluded(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    lar = await lc.observe(lesson="Laravel Breeze installs authentication via composer", scope="skill",
                           scope_ref="laravel", evidence_level=5)
    dock = await lc.observe(lesson="Docker system prune reclaims disk space", scope="environment",
                            evidence_level=5)
    await _force_state(live_pool, lar, "promoted")
    await _force_state(live_pool, dock, "promoted")
    hits = await relevant_knowledge(live_pool, objective="build a Laravel app with authentication")
    lessons = " ".join(h["lesson"] for h in hits)
    assert "Laravel" in lessons and "Docker" not in lessons


async def test_contradicted_knowledge_is_excluded_from_retrieval(live_pool: Any) -> None:
    lc = LearningCandidateStore(live_pool)
    good = await lc.observe(lesson="Laravel uses Artisan for migrations", scope="skill",
                            scope_ref="laravel", evidence_level=5)
    bad = await lc.observe(lesson="Laravel migrations need a manual SQL step", scope="skill",
                           scope_ref="laravel", evidence_level=5)
    await _force_state(live_pool, good, "verified")
    await _force_state(live_pool, bad, "contradicted")
    hits = await relevant_knowledge(live_pool, objective="run Laravel migrations")
    lessons = [h["lesson"] for h in hits]
    assert any("Artisan" in x for x in lessons) and not any("manual SQL" in x for x in lessons)


def test_render_hints_is_bounded() -> None:
    items = [{"lesson": "x" * 1000, "verification_state": "promoted", "source_type": None}
             for _ in range(10)]
    out = render_hints(items)
    assert out.count("\n- ") <= 5 and len(out) < 1600   # participates in the budget, never floods it (§33)


# ── Cognitive OS integration: learning state is part of the derived snapshot (§32) ──────────────────

async def test_cognitive_state_includes_bounded_learning(live_pool: Any) -> None:
    from sali.runtime import cognitive
    await LearningCandidateStore(live_pool).observe(lesson="a durable lesson", scope="task",
                                                    evidence_level=2)
    state = await cognitive.assemble(live_pool, session_id=uuid4())
    snap = state.snapshot()
    assert "active_learning" in snap and "learning_health" in snap and "open_contradictions" in snap
    assert snap["recent_learning"] and snap["recent_learning"][0]["lesson"] == "a durable lesson"
    assert len(snap["recent_learning"]) <= 5   # bounded — never the whole learning DB in the snapshot
