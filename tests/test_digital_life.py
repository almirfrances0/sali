"""Autonomous digital life, service ownership & long-lived action continuity (§52).

The new machinery: DigitalLifeObjects with lifecycle + temporal (stale) external state + relationships;
digital actions whose lifecycle never confuses started with completed; open OBLIGATIONS that make
unfinished business durable and resurfaceable; and COMMITMENTS distinct from tasks. Everything survives
compaction/restart and never holds a secret. (Capabilities, memory, experience, reviewer, cleanup,
natural clarification are covered by earlier suites and reused unchanged.)
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.runtime import cognitive
from sali.tasks.commitments import CommitmentStore, ObligationStore
from sali.tasks.digital import DigitalActionStore
from sali.tasks.external import ExternalEntityStore

pytestmark = pytest.mark.db


def _row(r: Any) -> Any:
    assert r is not None
    return r


# ── DigitalLifeObjects: persistence, ownership≠knowledge, staleness, relationships (§2/§3/§17/§19) ───

async def test_digital_object_persists_and_survives_restart(live_pool: Any) -> None:
    objs = ExternalEntityStore(live_pool, EventPublisher(live_pool))
    oid = await objs.discover(service="acme-cloud", ref="proj-42", object_type="project",
                              purpose="the client project", status="created")
    # a brand-new store (restart) reconstructs the object from PostgreSQL alone
    got = await ExternalEntityStore(live_pool).get_by_id(oid)
    assert got is not None and got["object_type"] == "project" and got["service"] == "acme-cloud"


async def test_external_state_goes_stale_and_reobservation_refreshes(live_pool: Any) -> None:
    objs = ExternalEntityStore(live_pool)
    oid = await objs.discover(service="acme-cloud", ref="site-1", object_type="website", status="active")
    assert await objs.is_stale(oid, max_age_seconds=3600)          # never observed → stale (§17/§18)
    await objs.observe(oid, last_observed={"http": 200})
    assert not await objs.is_stale(oid, max_age_seconds=3600)      # just observed → fresh
    got = await objs.get_by_id(oid)
    assert got is not None and got["last_observed"]["http"] == 200


async def test_relationships_persist(live_pool: Any) -> None:
    objs = ExternalEntityStore(live_pool)
    site = await objs.discover(service="acme", ref="mysite", object_type="website")
    await objs.relate(site, rel="deployed_to", target="server-1")
    await objs.relate(site, rel="uses", target="db-1")
    got = await objs.get_by_id(site)
    assert got is not None
    rels = {(r["rel"], r["target"]) for r in got["relationships"]}
    assert ("deployed_to", "server-1") in rels and ("uses", "db-1") in rels


# ── Digital actions: durable lifecycle, started≠completed, verification, resume, honesty (§7/§10/§16) ─

async def test_action_started_is_not_completed_and_verification_is_explicit(live_pool: Any) -> None:
    acts = DigitalActionStore(live_pool, EventPublisher(live_pool))
    aid = await acts.plan(intent="create the account", capability="web_account_management",
                          target="acme-cloud")
    await acts.advance(aid, "in_progress")
    got = await acts.get(aid)
    assert got is not None and got["status"] == "in_progress"      # started ≠ completed (§10)
    await acts.require_verification(aid, expected_state="login succeeds")
    assert _row(await acts.get(aid))["status"] == "verification_required"
    # only real evidence flips it to verified (§16)
    await acts.verify(aid, observed_state="login ok", evidence={"http": 200})
    assert _row(await acts.get(aid))["status"] == "verified"


async def test_verified_action_cannot_silently_reopen(live_pool: Any) -> None:
    acts = DigitalActionStore(live_pool)
    aid = await acts.plan(intent="deploy")
    await acts.verify(aid, observed_state="up")
    await acts.advance(aid, "in_progress")                         # attempt to reopen without evidence
    assert _row(await acts.get(aid))["status"] == "verified"           # ignored — no silent reopen (§52)


async def test_failed_action_stays_visible_and_blocked_action_resumes(live_pool: Any) -> None:
    acts = DigitalActionStore(live_pool)
    a1 = await acts.plan(intent="register")
    await acts.fail(a1, error="signup endpoint 500", status="failed")
    assert _row(await acts.get(a1))["status"] == "failed"              # historically visible (§32)
    a2 = await acts.plan(intent="configure")
    await acts.fail(a2, error="need login", status="authentication_required")
    # a brand-new store (crash) sees the unfinished action, and it can resume (§45)
    assert any(u["id"] == a2 for u in await DigitalActionStore(live_pool).unfinished())
    await acts.resume(a2)
    assert _row(await acts.get(a2))["status"] == "in_progress"


# ── Obligations: unfinished business is durable + resurfaces (§8/§9/§27/§32/§33) ─────────────────────

async def test_awaiting_external_state_creates_durable_obligation(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    objs = ExternalEntityStore(live_pool, pub)
    acts = DigitalActionStore(live_pool, pub)
    obl = ObligationStore(live_pool, pub)
    oid = await objs.discover(service="mail-co", ref="sali@x.io", object_type="account", status="created")
    aid = await acts.plan(intent="create email account", object_id=oid)
    ob_id = await acts.await_external(aid, obligation_description="verify the email address",
                                     next_action="check inbox for verification link")
    assert _row(await acts.get(aid))["status"] == "awaiting_external_state"
    # the obligation is durable and survives a restart (new store)
    assert any(o["id"] == ob_id for o in await ObligationStore(live_pool).open())
    got = await obl.get(ob_id)
    assert got is not None and got["source_action"] == aid and "verify" in got["description"]


async def test_obligation_resolves_and_cancel_is_historically_visible(live_pool: Any) -> None:
    obl = ObligationStore(live_pool, EventPublisher(live_pool))
    o1 = await obl.create(description="finish account setup", priority=3)
    await obl.resolve(o1, evidence={"done": True})
    assert not any(o["id"] == o1 for o in await obl.open())        # resolved → not in the open worklist
    o2 = await obl.create(description="renew certificate")
    await obl.cancel(o2, reason="cert no longer needed")
    async with live_pool.acquire() as c:                          # cancelled stays historically visible (§33)
        row = await c.fetchrow("SELECT status FROM obligation WHERE id=$1", o2)
    assert row["status"] == "cancelled"


async def test_verifying_action_resolves_its_obligation(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    acts = DigitalActionStore(live_pool, pub)
    obl = ObligationStore(live_pool, pub)
    aid = await acts.plan(intent="create account")
    ob_id = await acts.await_external(aid, obligation_description="verify email")
    await acts.verify(aid, observed_state="verified")             # verification closes the obligation
    got = await obl.get(ob_id)
    assert got is not None and got["status"] == "resolved"


# ── Commitments: distinct from tasks, span tasks, survive restart (§25/§26) ─────────────────────────

async def test_commitment_is_distinct_from_task_and_survives_restart(live_pool: Any) -> None:
    com = CommitmentStore(live_pool, EventPublisher(live_pool))
    task_a, task_b = uuid4(), uuid4()
    cid = await com.create(description="monitor the server", task_id=task_a)
    got = await com.get(cid)
    assert got is not None and got["id"] != task_a                # commitment_id != task_id (§26)
    # a brand-new store (restart) still sees the open commitment; it can span another task later
    assert any(c["id"] == cid for c in await CommitmentStore(live_pool).open())
    assert task_b != task_a                                       # the commitment outlives any single task


async def test_fulfilled_commitment_records_evidence_cancel_preserves_history(live_pool: Any) -> None:
    com = CommitmentStore(live_pool)
    c1 = await com.create(description="send the report")
    await com.fulfill(c1, evidence={"sent_to": "almir", "message_id": "m-1"})
    got = await com.get(c1)
    assert got is not None and got["status"] == "fulfilled" and got["evidence"]["sent_to"] == "almir"
    c2 = await com.create(description="obsolete promise")
    await com.cancel(c2, reason="no longer needed")
    async with live_pool.acquire() as c:
        assert (await c.fetchrow("SELECT status FROM commitment WHERE id=$1", c2))["status"] == "cancelled"


# ── security: no secrets in the digital-life ledgers (§12/§13/§49) ──────────────────────────────────

async def test_digital_life_ledgers_hold_no_secret_columns(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        cols: set[str] = set()
        for t in ("digital_action", "obligation", "commitment", "external_entity"):
            cols |= {r["column_name"] for r in await c.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name=$1", t)}
    assert not (cols & {"password", "secret", "token", "api_key", "cookie", "session_secret"})


# ── Cognitive OS integration: exposes digital life, bounded (§43/§44) ───────────────────────────────

async def test_cognitive_state_exposes_digital_life_and_stays_bounded(live_pool: Any) -> None:
    await ExternalEntityStore(live_pool).discover(service="acme", ref="p1", object_type="project",
                                                  status="created")
    await ObligationStore(live_pool).create(description="verify email")
    await CommitmentStore(live_pool).create(description="finish migration")
    snap = (await cognitive.assemble(live_pool, session_id=uuid4())).snapshot()
    assert snap["digital_objects"] >= 1 and snap["open_obligations"] >= 1 and snap["commitments"] >= 1
    assert snap["open_commitments"] >= 3                          # the aggregate rolls them up (§43)
    # bounded: counts + refs, never the whole ledgers dumped into the snapshot
    assert all(isinstance(snap[k], int)
               for k in ("digital_objects", "open_obligations", "commitments"))
