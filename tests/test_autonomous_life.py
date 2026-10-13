"""Autonomous life, initiative & the digital world (§73).

The new orchestration: durable hierarchical GOALS; the INITIATIVE ENGINE that turns durable signals into
deduplicated, deterministically-scored candidates with backoff (an observation is never an obligation, an
opportunity never permission); intentional ROUTINES; a wake/next-wake model; the AUTHORITY model
(capability ≠ authority); a digital-world ADAPTER protocol that advertises only real adapters; and
evidence-grounded PERSON state. Everything survives restart and is bounded. (Commitments, obligations,
digital actions, capability acquisition, experience, the 0-or-1 subagent, and restart/compaction are
covered by the earlier suites and reused unchanged.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.core.clock import FrozenClock
from sali.events.publisher import EventPublisher
from sali.runtime import cognitive
from sali.runtime.authority import AuthorityLevel, classify_action, is_autonomous
from sali.runtime.initiative import InitiativeBudget, InitiativeEngine, score
from sali.tasks.commitments import CommitmentStore, ObligationStore
from sali.tasks.goals import GoalStore
from sali.tasks.initiatives import InitiativeStore
from sali.tasks.people import PersonStore
from sali.tasks.routines import RoutineStore

pytestmark = pytest.mark.db


# ── goals: hierarchy, progress, retrievable history (§5/§6/§73.5-8) ─────────────────────────────────

async def test_goal_hierarchy_and_progress_survive_restart(live_pool: Any) -> None:
    goals = GoalStore(live_pool, EventPublisher(live_pool))
    top = await goals.create(objective="Maintain my dev environment", origin="self_initiated", priority=2)
    sub = await goals.create(objective="monitor important services", parent_goal=top, origin="self_initiated")
    await goals.update_progress(sub, progress=0.5, next_action="check nginx")
    # a brand-new store (restart) reconstructs the hierarchy + progress from PostgreSQL alone
    fresh = GoalStore(live_pool)
    subs = await fresh.subgoals(top)
    assert any(s["id"] == sub and s["progress"] == 0.5 for s in subs)
    got = await fresh.get(sub)
    assert got is not None and got["origin"] == "self_initiated" and got["next_action"] == "check nginx"


async def test_completed_goal_remains_retrievable(live_pool: Any) -> None:
    goals = GoalStore(live_pool)
    g = await goals.create(objective="Build the website", origin="user")
    await goals.complete(g, evidence={"deployed": True})
    got = await goals.get(g)
    assert got is not None and got["status"] == "completed" and got["progress"] == 1.0  # historically retrievable
    assert not any(x["id"] == g for x in await goals.active())     # not in the active worklist


# ── initiative engine: dedup, deterministic scoring, backoff, no duplicate tasks (§9-§12/§14/§40) ────

async def test_initiative_engine_generates_deduped_scored_candidates(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    com = CommitmentStore(live_pool, pub)
    # an overdue commitment is a durable signal — the engine should notice it
    await com.create(description="ship the report", deadline=datetime.now(UTC) - timedelta(hours=1))
    engine = InitiativeEngine(live_pool, pub)
    first = await engine.generate_candidates()
    assert first                                                    # at least one initiative created
    second = await engine.generate_candidates()                    # running again must NOT duplicate (§14)
    inits = InitiativeStore(live_pool)
    async with live_pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM initiative WHERE source='commitment'")
    assert n == 1 and set(second) == set(first)                    # same initiative, refreshed not duplicated
    top = await inits.top()
    assert top and 0.0 <= top[0]["priority_score"] <= 1.0 and top[0]["reason_codes"]  # scored + observable
    # no task was created merely by noticing (§12/§73.12)
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM task") == 0


async def test_initiative_engine_scans_all_durable_signal_sources(live_pool: Any) -> None:
    from sali.tasks.external import ExternalEntityStore
    pub = EventPublisher(live_pool)
    # an obligation due now, an active goal with a next action, and a stale active external object
    await ObligationStore(live_pool, pub).create(description="verify email",
                                                 next_check=datetime.now(UTC) - timedelta(minutes=1))
    await GoalStore(live_pool, pub).create(objective="learn kubernetes", origin="self_initiated",
                                           next_action="read the docs")
    oid = await ExternalEntityStore(live_pool, pub).discover(service="site", ref="s1",
                                                            object_type="website", status="active")
    async with live_pool.acquire() as c:  # force the object to look stale
        await c.execute("UPDATE external_entity SET last_verified = now() - interval '2 days' WHERE id=$1", oid)
    await InitiativeEngine(live_pool, pub).generate_candidates()
    sources = {i["source"] for i in await InitiativeStore(live_pool).open()}
    assert {"obligation", "goal", "environment"} <= sources          # every signal source noticed (§9/§11)


async def test_should_back_off_is_false_without_prior_attempts(live_pool: Any) -> None:
    engine = InitiativeEngine(live_pool, budget=InitiativeBudget(max_backoff_attempts=3))
    assert not await engine.should_back_off(source="goal", subject_ref="never-seen")


def test_authority_ladder_covers_every_level() -> None:
    assert classify_action(consequential=True, reversible=True, forbidden=True).level is AuthorityLevel.NOT_AUTHORIZED
    assert classify_action(consequential=True, reversible=True,
                           standing_authorization=True).level is AuthorityLevel.STANDING_AUTHORIZATION
    assert classify_action(consequential=True, reversible=True,
                           has_commitment_authority=True).level is AuthorityLevel.COMMITMENT_SCOPED
    # consequential-but-reversible with no scope → the human still decides
    d = classify_action(consequential=True, reversible=True)
    assert d.level is AuthorityLevel.REQUIRES_HUMAN and not is_autonomous(d.level)


def test_initiative_scoring_is_deterministic() -> None:
    a = score(source="commitment", urgency=1.0, risk=0.2)
    b = score(source="commitment", urgency=1.0, risk=0.2)
    assert a == b                                                   # deterministic (§10)
    # importance-by-source leads: a commitment outranks a low-importance environment nudge at equal urgency
    assert score(source="commitment", urgency=0.5, risk=0.2)["priority_score"] > \
        score(source="environment", urgency=0.5, risk=0.2)["priority_score"]


async def test_initiative_backoff_on_repeated_failure(live_pool: Any) -> None:
    inits = InitiativeStore(live_pool)
    iid, _ = await inits.upsert_candidate(source="goal", subject_ref="g1", title="try thing")
    for _ in range(3):
        await inits.record_attempt(iid, success=False, next_attempt=datetime.now(UTC) + timedelta(hours=1))
    engine = InitiativeEngine(live_pool, budget=InitiativeBudget(max_backoff_attempts=3))
    assert await engine.should_back_off(source="goal", subject_ref="g1")   # stop retrying (§40)
    assert not await inits.top()   # its backoff window hasn't elapsed → excluded from the worklist


# ── authority: capability ≠ authority (§52/§53/§73.17) ──────────────────────────────────────────────

def test_capability_is_not_authority() -> None:
    # an irreversible, consequential action needs a human decision — even if Sali *could* do it
    d = classify_action(consequential=True, reversible=False)
    assert d.level is AuthorityLevel.REQUIRES_HUMAN and not d.allowed_autonomously
    # a reversible, non-consequential action (observe/research) is safe autonomous
    safe = classify_action(consequential=False, reversible=True)
    assert safe.level is AuthorityLevel.SAFE_AUTONOMOUS and is_autonomous(safe.level)
    # explicit request or standing authorization always authorizes
    assert classify_action(consequential=True, reversible=True, explicitly_requested=True).allowed_autonomously
    # task authority covers reversible work; a consequential-irreversible action still needs the human
    assert classify_action(consequential=False, reversible=True, has_task_authority=True).allowed_autonomously
    assert not classify_action(consequential=True, reversible=False, has_task_authority=True).allowed_autonomously


# ── routines: intentional recurring, due, failure streak, next wake (§8/§37) ────────────────────────

async def test_routine_is_due_and_tracks_failure_streak(live_pool: Any) -> None:
    clock = FrozenClock(datetime(2026, 6, 1, tzinfo=UTC))
    routines = RoutineStore(live_pool, EventPublisher(live_pool), clock=clock)
    rid = await routines.create(name="check-health", when="30m", purpose="monitor services")
    assert not await routines.due()                                # not yet due
    clock.advance(31 * 60)
    due = await routines.due()
    assert any(r["id"] == rid for r in due)                        # now due (§37 wake signal)
    await routines.mark_executed(rid, success=False, result="db unreachable")
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT failure_count FROM routine WHERE id=$1", rid))["failure_count"] == 1
    await routines.mark_executed(rid, success=True)               # a success resets the streak
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT failure_count FROM routine WHERE id=$1", rid))["failure_count"] == 0


async def test_next_wake_is_derived_from_durable_state(live_pool: Any) -> None:
    soon = datetime.now(UTC) + timedelta(minutes=5)
    await ObligationStore(live_pool).create(description="verify email", next_check=soon)
    wake = await InitiativeEngine(live_pool).next_wake()
    assert wake is not None and abs((wake - soon).total_seconds()) < 2   # earliest durable wake time (§37)


# ── person / relationship: durable + provenance (§23/§24) ───────────────────────────────────────────

async def test_person_relationship_is_durable_with_provenance(live_pool: Any) -> None:
    people = PersonStore(live_pool, EventPublisher(live_pool))
    await people.upsert(name="Almir", relationship_type="owner", provenance="explicit", confidence=0.95)
    await people.set_preference("Almir", key="channel", value="terminal", provenance="explicit")
    await people.record_interaction("Almir", note="discussed the roadmap")
    got = await PersonStore(live_pool).get("Almir")               # survives restart
    assert got is not None and got["relationship_type"] == "owner" and got["provenance"] == "explicit"
    assert got["preferences"]["channel"] == "terminal" and got["last_interaction"] is not None


# ── digital-world adapters: advertise only real adapters (§15/§33/§47) ──────────────────────────────

def test_adapter_registry_advertises_only_real_adapters() -> None:
    from sali.tools.adapters import AdapterRegistry, DigitalAdapter

    reg = AdapterRegistry()
    assert reg.names() == [] and reg.advertised_capabilities() == {}   # nothing advertised until built

    class _FakeAdapter:
        name = "fake"

        def capabilities(self) -> list[str]:
            return ["observe_fake"]

        async def discover(self) -> dict[str, Any]:
            return {}

        async def observe(self, target: str) -> dict[str, Any]:
            return {}

        async def act(self, intent: str, params: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def verify(self, target: str, expected: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reconcile(self, target: str) -> dict[str, Any]:
            return {}

    adapter = _FakeAdapter()
    assert isinstance(adapter, DigitalAdapter)                     # satisfies the protocol
    reg.register(adapter)
    assert reg.advertised_capabilities() == {"fake": ["observe_fake"]}


# ── Cognitive OS integration: exposes autonomous life, bounded (§78) ────────────────────────────────

async def test_cognitive_state_exposes_autonomous_life(live_pool: Any) -> None:
    await GoalStore(live_pool).create(objective="learn kubernetes", origin="self_initiated")
    await RoutineStore(live_pool).create(name="daily-review", when="1d")
    await PersonStore(live_pool).upsert(name="Almir", relationship_type="owner")
    await InitiativeStore(live_pool).upsert_candidate(source="goal", subject_ref="g", title="x")
    snap = (await cognitive.assemble(live_pool, session_id=uuid4())).snapshot()
    assert snap["active_goals"] >= 1 and snap["open_initiatives"] >= 1 and snap["known_people"] >= 1
    assert "next_wakeup" in snap and "routines_due" in snap
    assert all(isinstance(snap[k], int) for k in ("active_goals", "open_initiatives", "known_people"))
