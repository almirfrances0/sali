"""Human interaction, judgment & natural consent (§69).

The new layer: a deterministic JUDGMENT model over structured dimensions (reversibility/scope/externality/
…) that produces decision signals (low→blocked), never hardcoded prohibitions or y/n gates; and a
natural CONSENT model where the user's free-text reply is the consent signal — scoped, expiring,
revocable, inheritable. Capability ≠ authority ≠ consequence stay separate axes. (Conversation-vs-work
routing, interruption, memory/provenance, commitments, external identities, and the reviewer are covered
by the earlier suites and reused unchanged.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.runtime import cognitive
from sali.runtime.judgment import CapabilityAction, JudgmentLevel, assess
from sali.tasks.consent import ConsentStore, interpret_consent_response
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _row(r: Any) -> Any:
    assert r is not None
    return r


# ── judgment over structured dimensions (§7/§8/§73) — pure, no DB ────────────────────────────────────

def test_judgment_levels_from_dimensions() -> None:
    read = CapabilityAction(capability="filesystem", action="read", reversible=True, external=False)
    assert assess(read).level is JudgmentLevel.LOW and not assess(read).needs_consent
    # ordinary task work within authority proceeds without consent
    edit = CapabilityAction(capability="filesystem", action="modify", reversible=True)
    assert assess(edit, has_task_authority=True).level in (JudgmentLevel.LOW, JudgmentLevel.NORMAL)
    # irreversible / external actions require natural consent
    delete = CapabilityAction(capability="filesystem", action="delete", reversible=False, destructive=True)
    d = assess(delete)
    assert d.level is JudgmentLevel.HIGH_CONSEQUENCE and d.needs_consent
    email = CapabilityAction(capability="email", action="send", external=True)
    assert assess(email).level is JudgmentLevel.CONSENT and assess(email).needs_consent
    # financial / public → high consequence
    pay = CapabilityAction(capability="payments", action="charge", financial=True)
    assert assess(pay).level is JudgmentLevel.HIGH_CONSEQUENCE
    post = CapabilityAction(capability="social", action="post", external=True, public=True)
    assert assess(post).level is JudgmentLevel.HIGH_CONSEQUENCE
    # ambiguous target asks for attention, not blind action
    ambiguous = CapabilityAction(capability="filesystem", action="open", ambiguous_target=True)
    assert assess(ambiguous).level is JudgmentLevel.ATTENTION


def test_capability_absence_and_policy_are_blocked_not_consent() -> None:
    act = CapabilityAction(capability="social", action="post", external=True, public=True)
    assert assess(act, capability_available=False).level is JudgmentLevel.BLOCKED   # honest absence (§28)
    assert assess(act, forbidden=True).level is JudgmentLevel.BLOCKED               # policy boundary (§8)


def test_existing_authorization_suppresses_redundant_consent() -> None:
    delete = CapabilityAction(capability="filesystem", action="delete", reversible=False, destructive=True)
    # the user explicitly asked for exactly this → no redundant consent (§9/§13)
    d = assess(delete, explicitly_requested=True)
    assert not d.needs_consent and "already authorized by the user" in d.reasons
    # a standing authorization also suppresses it
    assert not assess(delete, standing_authorization=True).needs_consent


# ── natural-language consent interpretation (§4/§17) — pure ─────────────────────────────────────────

def test_interpret_consent_response_ordering() -> None:
    assert interpret_consent_response("yes, do it").status == "granted"
    assert interpret_consent_response("go ahead").status == "granted"
    assert interpret_consent_response("only the first two").status == "modified"
    assert interpret_consent_response("go ahead with the safe option").status == "modified"
    assert interpret_consent_response("not now").status == "deferred"       # 'not now' ≠ 'no' (§15)
    assert interpret_consent_response("leave it").status == "declined"
    assert interpret_consent_response("no, don't").status == "declined"
    assert interpret_consent_response("what exactly will be deleted?").status == "unclear"


# ── consent lifecycle: request → natural resolution, scoped, restart-safe (§4/§5/§57) ───────────────

async def test_consent_request_and_natural_resolution(live_pool: Any) -> None:
    store = ConsentStore(live_pool, EventPublisher(live_pool))
    store2 = TaskStore(live_pool)
    t = await store2.create("Free up disk space", ["cleanup"])
    cid = await store.request(action="remove 3 old build directories (18 GB)", scope="/builds",
                              consequence="irreversible unless backed up", judgment_level="consent",
                              reversible=False, task_id=t.id)
    pend = await store.pending(t.id)
    assert pend is not None and pend["id"] == cid                    # pending, structured state (§57)
    outcome = await store.resolve(cid, "yes, remove them")
    assert outcome.status == "granted"
    assert _row(await store.get(cid))["status"] == "granted"
    assert await store.pending(t.id) is None                        # resolved → no longer pending


async def test_consent_modified_scope_and_decline(live_pool: Any) -> None:
    store = ConsentStore(live_pool)
    c1 = await store.request(action="delete build dirs", scope="all")
    assert (await store.resolve(c1, "only remove the two oldest")).status == "modified"
    got = _row(await store.get(c1))
    assert got["status"] == "modified" and "two oldest" in got["granted_scope"]   # narrowed scope (§5)
    c2 = await store.request(action="delete build dirs", scope="all")
    assert (await store.resolve(c2, "leave them for now")).status == "deferred"    # 'for now' → defer


async def test_unclear_reply_keeps_consent_pending(live_pool: Any) -> None:
    store = ConsentStore(live_pool)
    t = await TaskStore(live_pool).create("x", ["a"])
    cid = await store.request(action="publish the site publicly", task_id=t.id)
    assert (await store.resolve(cid, "why do you think that's safe?")).status == "unclear"
    assert await store.pending(t.id) is not None                    # still pending — Sali can ask again (§71)


async def test_consent_is_scoped_not_unlimited(live_pool: Any) -> None:
    store = ConsentStore(live_pool)
    # a granted one-off consent does NOT create a standing authorization (§5/§14)
    c = await store.request(action="delete temp file", scope="/tmp/x")
    await store.resolve(c, "yes")
    assert not await store.has_standing_authorization(scope="/tmp/x")


async def test_standing_authorization_grant_and_revoke(live_pool: Any) -> None:
    store = ConsentStore(live_pool, EventPublisher(live_pool))
    await store.grant_standing(scope="workspace-files", action="create files in the task workspace")
    assert await store.has_standing_authorization(scope="workspace-files")       # §38
    assert await store.revoke_standing(scope="workspace-files") == 1             # "don't do that anymore" (§39)
    assert not await store.has_standing_authorization(scope="workspace-files")


async def test_consent_expires_and_is_not_reused(live_pool: Any) -> None:
    store = ConsentStore(live_pool)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await store.grant_standing(scope="send-emails", action="send emails")
    async with live_pool.acquire() as c:                            # give it a past expiry
        await c.execute("UPDATE consent_request SET expires_at=$1 WHERE scope='send-emails'", past)
    assert not await store.has_standing_authorization(scope="send-emails")       # expired → not usable (§58)
    assert await store.expire_stale() >= 1


async def test_pending_consent_survives_restart_and_shows_in_cognitive_state(live_pool: Any) -> None:
    store = ConsentStore(live_pool)
    tasks = TaskStore(live_pool)
    t = await tasks.create("Publish the project", ["publish"])
    await tasks.activate(t.id)
    await store.request(action="publish the site publicly", scope="internet",
                        consequence="the site becomes publicly accessible", judgment_level="high_consequence",
                        task_id=t.id)
    # a brand-new store (restart) still sees the pending consent
    assert await ConsentStore(live_pool).pending(t.id) is not None
    snap = (await cognitive.assemble(live_pool, loop=_LoopStub(TaskStore(live_pool)),
                                     session_id=uuid4())).snapshot()
    assert snap["pending_consent"] is not None and "publicly" in snap["pending_consent"]["consequence"]


class _LoopStub:
    """Minimal loop stand-in for cognitive.assemble — enough to derive the primary-task-scoped state."""

    def __init__(self, store: TaskStore) -> None:
        self._tasks = store
        self._reviewer = self._research_store = self._skills = self._decisions = self._phases = None

        class _S:
            async def assemble(self_i: Any) -> dict[str, Any]:
                return {}
        self._self_state = _S()

        class _R:
            def advertise(self_i: Any) -> list[Any]:
                return []
        self.registry = _R()


async def test_resolve_pending_for_task_is_the_handle_message_path(live_pool: Any) -> None:
    # handle_message calls resolve_pending_for_task with the user's reply; None when nothing is pending
    store = ConsentStore(live_pool, EventPublisher(live_pool))
    tasks = TaskStore(live_pool)
    t = await tasks.create("Deploy", ["deploy"])
    assert await store.resolve_pending_for_task(t.id, "yes go ahead") is None    # ordinary msg, no consent
    await store.request(action="deploy to production", task_id=t.id)
    outcome = await store.resolve_pending_for_task(t.id, "yes, go ahead")
    assert outcome is not None and outcome.status == "granted"
