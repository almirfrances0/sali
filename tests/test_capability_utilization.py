"""Capability utilization, durable competence & purposeful acquisition (§32).

The invariant: Sali must never acquire a meaningful capability and then behave as though it never
happened. A learned/verified capability becomes a durable, discoverable, USABLE part of Sali's
operational world — with a purpose, a backing resource where relevant, usage history, and honest
freshness — never a dead entry or a claim built from merely reading documentation. (The capability
lifecycle, confidence, acquisition, and accounts-as-resources are the earlier suites, reused unchanged.)
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.capability import CapabilityStore
from sali.tasks.external import ExternalEntityStore

pytestmark = pytest.mark.db


# ── knowledge ≠ capability: reading docs does not create ability (§2/§11/§24) ───────────────────────

async def test_documented_capability_is_not_usable(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.observe(name="use the widgets API", scope="environment", scope_ref="kali",
                       status="researched")   # knowledge only — read the docs
    assert not await caps.already_have(name="use the widgets API", scope_ref="kali")
    assert await caps.check_availability(name="use the widgets API", scope_ref="kali") == "unavailable"


async def test_failed_acquisition_is_not_an_available_capability(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="install a broken tool", success=False, scope_ref="kali")
    assert await caps.check_availability(name="install a broken tool", scope_ref="kali") == "unavailable"


# ── purpose + resource link: acquisition enters the operational world (§4/§6/§15/§19) ────────────────

async def test_capability_records_purpose_and_backing_resource(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool, EventPublisher(live_pool))
    objs = ExternalEntityStore(live_pool, EventPublisher(live_pool))
    account = await objs.discover(service="social", ref="proj-page", object_type="account", status="active",
                                  purpose="project presence")
    await caps.record_attempt(name="publish to the project social page", success=True, verified=True,
                              scope_ref="kali")
    await caps.set_purpose(name="publish to the project social page",
                           purpose="publish project updates through the approved channel",
                           scope_ref="kali", resource_id=account)
    got = await caps.get(name="publish to the project social page", scope="environment", scope_ref="kali")
    assert got is not None and got["resource_id"] == account
    assert "approved channel" in got["purpose"]   # purpose survives the acquisition task (§19)


async def test_partial_resource_is_not_fully_available(live_pool: Any) -> None:
    objs = ExternalEntityStore(live_pool)
    eid = await objs.discover(service="mail", ref="a@b.c", object_type="account", status="created")
    got = await objs.get_by_id(eid)
    # account creation started ≠ account available (§24) — verification is still pending
    assert got is not None and got["status"] == "created" and got["verification_state"] == "unverified"


# ── utilization: use it, remember using it, prefer it over relearning (§10/§12/§16/§22) ──────────────

async def test_successful_use_updates_usage_history(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool, EventPublisher(live_pool))
    await caps.record_attempt(name="deploy with docker", success=True, verified=True, scope_ref="kali")
    used = await caps.record_use(name="deploy with docker", scope_ref="kali",
                                 purpose="deploy the app", action="docker compose up", result="up",
                                 success=True, verified=True)
    assert used
    got = await caps.get(name="deploy with docker", scope="environment", scope_ref="kali")
    assert got is not None and got["use_count"] == 1 and got["last_used"] is not None
    assert got["status"] == "available"   # a verified successful use promotes verified → available
    hist = await caps.usage_history(name="deploy with docker", scope_ref="kali")
    assert hist and hist[0]["action"] == "docker compose up" and hist[0]["success"] is True


async def test_recording_use_of_unknown_capability_returns_false(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    assert not await caps.record_use(name="never acquired", scope_ref="kali")   # no phantom usage (§24)


async def test_existing_capability_is_discovered_and_preferred_over_relearning(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="scaffold a Laravel app", success=True, verified=True, scope_ref="kali")
    # a later, unrelated task's planning finds the existing competence (§8/§9/§22)
    hits = await caps.discover_for_task("scaffold a new Laravel project quickly", scope_ref="kali")
    assert hits and any(h["name"] == "scaffold a Laravel app" for h in hits)
    assert await caps.already_have(name="scaffold a Laravel app", scope_ref="kali")   # don't relearn (§16)


# ── freshness: known ≠ recently verified ≠ currently available (§13) ────────────────────────────────

async def test_capability_freshness_states(live_pool: Any) -> None:
    caps = CapabilityStore(live_pool)
    await caps.record_attempt(name="connect to the VPS", success=True, verified=True, scope_ref="kali")
    assert await caps.check_availability(name="connect to the VPS", scope_ref="kali") == "available"
    # force the last verification into the past → the capability is known but STALE (verify before use)
    async with live_pool.acquire() as c:
        await c.execute("UPDATE capability SET last_verified = now() - interval '30 days' "
                        "WHERE name='connect to the VPS' AND scope_ref='kali'")
    assert await caps.check_availability(name="connect to the VPS", scope_ref="kali",
                                         max_age_seconds=86400) == "stale"
    # the dependency breaks → degraded (history preserved), never silently 'available'
    await caps.degrade(name="connect to the VPS", scope_ref="kali", reason="ssh key rotated")
    assert await caps.check_availability(name="connect to the VPS", scope_ref="kali") == "degraded"


# ── persistence: capability + resource survive task completion / cleanup / restart (§19/§20/§32) ─────

async def test_capability_and_resource_survive_task_completion_and_restart(live_pool: Any) -> None:
    from pathlib import Path

    from sali.learning.experience import ExperienceStore
    from sali.tasks.reviewer import TaskReviewer
    from sali.tasks.store import TaskStore

    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    store._reviewer = TaskReviewer(live_pool, pub)
    store._experience_hook = ExperienceStore(live_pool, pub).extract_and_persist
    caps = CapabilityStore(live_pool, pub)

    # acquire a capability + backing resource during a task with an ephemeral workspace
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        works = str(Path(tmp) / "sali-works")
        t = await store.create("Set up the deployment capability", ["setup"])
        await store.activate(t.id)
        bound = await store.bind_workspace(t.id, objective="setup", explicit=None, sali_works_root=works)
        ws = Path(bound["workspace_root"])
        await caps.record_attempt(name="deploy the app", success=True, verified=True, scope_ref="kali")
        await caps.set_purpose(name="deploy the app", purpose="ship releases", scope_ref="kali")
        ex = await store.record_execution(t.id, 1, "setup_tool", attempt=1)
        await store.complete_execution(ex, status="completed", result_summary="done")
        async with live_pool.acquire() as c:
            await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", t.id)
        assert await store.finish(t.id, status="done") is None
        assert not ws.exists()                       # ephemeral workspace cleaned

    # a brand-new store (restart) still has the usable capability with its purpose (§20/§32)
    fresh = CapabilityStore(live_pool)
    assert await fresh.already_have(name="deploy the app", scope_ref="kali")
    got = await fresh.get(name="deploy the app", scope="environment", scope_ref="kali")
    assert got is not None and got["purpose"] == "ship releases"


# ── security: the capability registry never carries secrets (§15/§30) ───────────────────────────────

async def test_capability_tables_hold_no_secret_columns(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        cols: set[str] = set()
        for t in ("capability", "capability_usage"):
            cols |= {r["column_name"] for r in await c.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name=$1", t)}
    assert not (cols & {"password", "secret", "token", "api_key", "credential", "cookie"})
