"""Agency, capability evolution & real-world action (§68).

The new machinery: capabilities compose (dependencies), gaps are identified rather than hallucinated,
acquisition is a durable resumable lifecycle, capabilities regress without erasing history, learned
capability is reusable ("I've done this before"), and external identities/accounts have a real
lifecycle distinct from memory — never holding secrets. Evidence, not the model, drives every promotion.
(Commitments, cleanup, natural clarification, memory, reviewer, restart are covered by the earlier
persistent-agency / lifetime-memory suites and reused unchanged.)
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.capability import CapabilityStore
from sali.learning.capability_acquisition import CapabilityAcquisitionStore
from sali.runtime import cognitive
from sali.tasks.external import ExternalEntityStore

pytestmark = pytest.mark.db


# ── capability model: discovery, dependencies, evidence, regression, history (§68.1-9) ──────────────

async def test_capability_discovered_dynamically_no_hardcoding(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool, EventPublisher(live_pool))
    # a capability nobody programmed as a feature — just discovered by name
    await caps.observe(name="operate a Kubernetes cluster", scope="environment", scope_ref="kali",
                       status="researched")
    got = await caps.get(name="operate a Kubernetes cluster", scope="environment", scope_ref="kali")
    assert got is not None and got["status"] == "researched"


async def test_failed_attempt_is_not_verified_verified_gains_confidence(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="deploy service", success=False, scope_ref="kali")
    failed = await caps.get(name="deploy service", scope="environment", scope_ref="kali")
    assert failed is not None and failed["status"] != "verified"        # §3/§68.6 — no false capability
    await caps.record_attempt(name="deploy service", success=True, verified=True, scope_ref="kali")
    ok = await caps.get(name="deploy service", scope="environment", scope_ref="kali")
    assert ok is not None and ok["status"] == "verified" and ok["confidence"] > failed["confidence"]  # §29


async def test_capability_dependencies_and_gap_analysis(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    # a composed capability declares what it needs (§8/§9)
    await caps.set_dependencies(name="deploy a website", depends_on=["shell", "git", "browser"],
                                scope_ref="kali")
    got = await caps.get(name="deploy a website", scope="environment", scope_ref="kali")
    assert got is not None and set(got["depends_on"]) == {"shell", "git", "browser"}
    # only 'shell' is actually a verified capability → the rest are a real, honest GAP (§7)
    await caps.record_attempt(name="shell", success=True, verified=True, scope_ref="kali")
    gap = await caps.gap_analysis(["shell", "git", "browser"], scope_ref="kali")
    assert gap["available"] == ["shell"] and set(gap["missing"]) == {"git", "browser"}


async def test_capability_regression_preserves_history(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool, EventPublisher(live_pool))
    await caps.record_attempt(name="build docker image", success=True, verified=True, scope_ref="kali")
    before = await caps.get(name="build docker image", scope="environment", scope_ref="kali")
    assert before is not None and before["times_succeeded"] == 1
    # the dependency disappears → the capability degrades, but its history is NOT erased (§30)
    assert await caps.degrade(name="build docker image", scope_ref="kali", reason="docker uninstalled")
    degraded = await caps.get(name="build docker image", scope="environment", scope_ref="kali")
    assert degraded is not None and degraded["status"] == "degraded" and degraded["times_succeeded"] == 1
    assert not await caps.is_usable(name="build docker image", scope_ref="kali")   # currently unavailable
    # docker comes back → capability restored, history appended (§71)
    assert await caps.restore(name="build docker image", scope_ref="kali")
    restored = await caps.get(name="build docker image", scope="environment", scope_ref="kali")
    assert restored is not None and restored["status"] == "verified" and restored["times_succeeded"] == 2


async def test_capability_history_survives_restart(live_pool: Any) -> None:
    await CapabilityStore(live_pool).record_attempt(name="run migrations", success=True, verified=True,
                                                    scope_ref="kali")
    # a brand-new store (restart) still knows the capability from PostgreSQL alone (§68.8)
    assert await CapabilityStore(live_pool).is_usable(name="run migrations", scope_ref="kali")


async def test_capability_reuse_i_have_done_this_before(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="deploy a Laravel application", success=True, verified=True,
                              scope_ref="kali")
    hits = await caps.for_goal("deploy a Laravel app to staging", scope_ref="kali")
    assert hits and hits[0]["name"] == "deploy a Laravel application"   # reuse (§38/§70)
    assert hits[0]["status"] == "verified"


# ── capability acquisition lifecycle: gap → research → acquire → verify, resumable (§68.10-18) ───────

async def test_acquisition_lifecycle_is_durable_and_resumable(live_pool: Any) -> None:
    acq = CapabilityAcquisitionStore(live_pool, EventPublisher(live_pool))
    aid = await acq.identify_gap(capability_name="use the GitHub API", missing=["github-api"],
                                 scope_ref="kali", notes="need a token")
    # re-identifying the SAME gap resumes the same acquisition, never duplicates (§53)
    aid2 = await acq.identify_gap(capability_name="use the GitHub API", scope_ref="kali")
    assert aid2 == aid
    await acq.advance(aid, "researching", notes="read the REST docs")
    await acq.advance(aid, "acquiring")
    # a brand-new store (restart) sees the in-progress acquisition and can continue (§62)
    assert any(o["id"] == aid for o in await CapabilityAcquisitionStore(live_pool).open())
    await acq.advance(aid, "verifying")
    await acq.acquired(aid)
    got = await acq.get(aid)
    assert got is not None and got["status"] == "acquired"
    assert not await acq.open()   # nothing left in progress


async def test_failed_acquisition_stays_resumable_and_is_no_false_capability(live_pool: Any) -> None:
    acq = CapabilityAcquisitionStore(live_pool)
    caps = CapabilityStore(live_pool)
    aid = await acq.identify_gap(capability_name="send SMS", scope_ref="kali")
    await acq.fail(aid, error="no gateway available")
    # the failure does NOT create a capability (§3/§42)
    assert await caps.get(name="send SMS", scope="environment", scope_ref="kali") is None
    # a fresh attempt can be started later — the gap is resumable (§15)
    aid2 = await acq.identify_gap(capability_name="send SMS", scope_ref="kali")
    fresh = await acq.get(aid2)
    assert aid2 != aid and fresh is not None and fresh["status"] == "gap_identified"


# ── external entities / accounts: lifecycle distinct from memory, evidence-based, no secrets (§11/§12) ─

async def test_external_account_incomplete_until_verified(live_pool: Any) -> None:
    ext = ExternalEntityStore(live_pool, EventPublisher(live_pool))
    eid = await ext.discover(service="email", ref="sali@example.com", purpose="project inbox",
                             authority="user", status="created")
    await ext.advance(eid, "verification_pending")
    got = await ext.get(service="email", ref="sali@example.com")
    assert got is not None and got["status"] == "verification_pending"
    assert got["verification_state"] == "unverified"        # a submitted form is NOT a verified account (§12)
    assert any(u["id"] == eid for u in await ext.unfinished())   # visible as unfinished, not abandoned (§35)
    # only real confirmation flips it to verified (§41)
    await ext.set_verification(eid, state="verified")
    done = await ext.get(service="email", ref="sali@example.com")
    assert done is not None and done["status"] == "verified" and done["verification_state"] == "verified"


async def test_external_entity_never_stores_secrets(live_pool: Any) -> None:
    ext = ExternalEntityStore(live_pool)
    eid = await ext.discover(service="github", ref="github.com/sali", purpose="repo")
    await ext.set_credential_configured(eid, configured=True)   # the token lives in the vault, not here
    got = await ext.get(service="github", ref="github.com/sali")
    assert got is not None and got["credential_configured"] is True
    # the row carries only a boolean flag — no secret column exists to leak (§11/§65)
    async with live_pool.acquire() as c:
        cols = {r["column_name"] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='external_entity'")}
    assert not (cols & {"credential", "secret", "token", "password"})


async def test_partial_external_workflow_survives_restart(live_pool: Any) -> None:
    ext = ExternalEntityStore(live_pool)
    await ext.discover(service="cloud", ref="proj-123", status="created")
    # a brand-new store (restart) still sees the half-finished external operation (§33)
    assert any(u["service"] == "cloud" for u in await ExternalEntityStore(live_pool).unfinished())


# ── Cognitive OS integration: commitments, acquisitions, externals exposed + bounded (§55/§68.47-54) ─

async def test_cognitive_state_exposes_agency_and_stays_bounded(live_pool: Any) -> None:
    await CapabilityAcquisitionStore(live_pool).identify_gap(capability_name="x", scope_ref="kali")
    await ExternalEntityStore(live_pool).discover(service="email", ref="a@b.c", status="created")
    state = await cognitive.assemble(live_pool, session_id=uuid4())
    snap = state.snapshot()
    assert "capability_acquisitions" in snap and snap["capability_acquisitions"] >= 1
    assert "external_entities" in snap and snap["external_entities"] >= 1
    assert "open_commitments" in snap and snap["open_commitments"] >= 2
    # bounded: the snapshot is compact counts + refs, never the full capability/entity tables
    assert isinstance(snap["capability_acquisitions"], int)


async def test_capability_overview_is_self_describing_no_fakery(live_pool: Any) -> None:
    from sali.tools.registry import default_registry
    await CapabilityStore(live_pool).record_attempt(name="deploy laravel", success=True, verified=True,
                                                    scope_ref="kali")
    overview = await cognitive.capability_overview(live_pool, default_registry())
    assert overview["builtin"] and all(g["kind"] == "builtin" for g in overview["builtin"])
    learned_names = {c["capability"] for c in overview["learned"]}
    assert "deploy laravel" in learned_names                 # learned capability merged in (§34)
    # a capability Sali never demonstrated is NOT advertised
    assert "operate a nuclear reactor" not in learned_names
