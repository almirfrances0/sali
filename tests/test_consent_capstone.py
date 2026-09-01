"""Consent capstone (§70) + adversarial recovery (§71).

"Build my web project and get it ready to publish." Sali does the ordinary subordinate work under task
authority (no confirmation spam), hits an unfamiliar dependency and researches it, then reaches the
publish step — which is externally public and therefore genuinely consequential. He does NOT treat the
build request as permission to publish: judgment escalates, he asks in natural language, and only the
user's free-text "yes, publish it" authorizes it. The action is then executed, verified, and the reviewer
gates completion. Adversarial: a restart while consent is pending/granted recovers deterministically;
'not now' defers without acting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from sali.events.publisher import EventPublisher
from sali.learning.experience import ExperienceStore
from sali.runtime.judgment import CapabilityAction, JudgmentLevel, assess
from sali.tasks.consent import ConsentStore
from sali.tasks.digital import DigitalActionStore
from sali.tasks.external import ExternalEntityStore
from sali.tasks.reviewer import TaskReviewer
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


async def test_consent_capstone(live_pool: Any, tmp_path: Path) -> None:
    works = tmp_path / "sali-works"
    pub = EventPublisher(live_pool)
    store, exp = _wired(live_pool)
    consent = ConsentStore(live_pool, pub)
    objs, acts = ExternalEntityStore(live_pool, pub), DigitalActionStore(live_pool, pub)

    # 1) the objective + ephemeral workspace
    t = await store.create("Build my web project and get it ready to publish", ["build", "publish"])
    await store.activate(t.id)
    task_id = t.id
    bound = await store.bind_workspace(t.id, objective="Build web project", explicit=None,
                                       sali_works_root=str(works))
    ws = Path(bound["workspace_root"])

    # 2) ordinary subordinate work under task authority — NO consent needed (no confirmation spam, §10)
    for tool in ("create_file", "install_dependency", "run_tests"):
        act = CapabilityAction(capability="filesystem", action=tool, reversible=True, scope="project")
        assert not assess(act, has_task_authority=True).needs_consent
        ex = await store.record_execution(t.id, 1, tool, attempt=1)
        await store.complete_execution(ex, status="completed", result_summary=f"{tool} ok")

    # 3) an unfamiliar dependency → research it (evidence, not a guess)
    from sali.tasks.research import ResearchStore
    await ResearchStore(live_pool, pub).record_research(
        task_id=t.id, run_id=None, step_seq=1, query="how to configure the new bundler",
        source="https://docs.example/bundler", summary="set BUNDLE_ENV=prod", confidence=0.8, content_hash="h")

    # 4) the PUBLISH step is external + public → judgment escalates → NATURAL CONSENT required (§7/§8/§60)
    publish = CapabilityAction(capability="web", action="publish", external=True, public=True, scope="public")
    decision = assess(publish, has_task_authority=True)   # building it is NOT permission to publish (§59)
    assert decision.level is JudgmentLevel.HIGH_CONSEQUENCE and decision.needs_consent
    site = await objs.discover(service="web-host", ref="myproj", object_type="website", status="created",
                               task_id=t.id)
    action = await acts.plan(intent="publish the site publicly", object_id=site, capability="web",
                             task_id=t.id)
    cid = await consent.request(action="publish the site publicly", scope="internet",
                                consequence="the site becomes publicly accessible on the internet",
                                rationale="the build is verified locally", judgment_level="high_consequence",
                                external=True, task_id=t.id)

    # 5) COMPACTION + RESTART while consent is pending — it survives and the site is NOT yet published
    from sali.runtime import cognitive
    snap = (await cognitive.assemble(live_pool, loop=_LoopStub(store), session_id=task_id)).snapshot()
    assert snap["pending_consent"] is not None
    assert _row(await DigitalActionStore(live_pool).get(action))["status"] == "planned"   # not published yet
    fresh_consent = ConsentStore(live_pool, pub)
    assert await fresh_consent.pending(task_id) is not None        # pending consent survives restart (§53)

    # 6) the user authorizes it in natural language → the consent resolves (no y/n, §4)
    outcome = await fresh_consent.resolve_pending_for_task(task_id, "yes, publish it")
    assert outcome is not None and outcome.status == "granted"
    assert _row(await consent.get(cid))["status"] == "granted"

    # 7) NOW publish, verify the external result, finish → reviewer PASS → experience + cleanup
    await acts.advance(action, "in_progress")
    await acts.verify(action, observed_state="https://myproj.example returns 200", evidence={"http": 200})
    assert _row(await acts.get(action))["status"] == "verified"
    await _complete_steps(live_pool, t.id)
    assert await store.finish(t.id, status="done") is None

    # ── assertions (§70) ────────────────────────────────────────────────────────────────────────────
    assert await store.get(task_id) is None                       # one task, completed + archived
    assert not ws.exists()                                        # ephemeral workspace cleaned
    assert any("Build my web project" in r["content"] for r in await exp.recent())   # experience recorded
    async with live_pool.acquire() as c:                          # consent WAS required before publishing
        assert await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='consent.requested'") >= 1
        assert await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='consent.granted'") >= 1


# ── §71 adversarial ─────────────────────────────────────────────────────────────────────────────────

async def test_restart_after_consent_granted_resumes_the_action(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    consent, acts = ConsentStore(live_pool, pub), DigitalActionStore(live_pool, pub)
    tasks = TaskStore(live_pool)
    t = await tasks.create("Send the report", ["send"])
    action = await acts.plan(intent="send the report email", capability="email", task_id=t.id)
    cid = await consent.request(action="send the report email", external=True, task_id=t.id)
    await consent.resolve(cid, "yes go ahead")                    # granted, then "crash"
    # a brand-new store set (restart) sees the granted consent + the still-unfinished action → resume
    assert _row(await ConsentStore(live_pool).get(cid))["status"] == "granted"
    assert any(u["id"] == action for u in await DigitalActionStore(live_pool).unfinished())


async def test_not_now_defers_without_acting_and_can_be_re_requested(live_pool: Any) -> None:
    consent = ConsentStore(live_pool)
    tasks = TaskStore(live_pool)
    t = await tasks.create("Deploy", ["deploy"])
    c1 = await consent.request(action="deploy to production", external=True, task_id=t.id)
    assert (await consent.resolve(c1, "not now")).status == "deferred"   # deferred, not acted (§15)
    assert await consent.pending(t.id) is None                    # no longer pending
    # later, Sali can ask again — a fresh consent request for the same action
    c2 = await consent.request(action="deploy to production", external=True, task_id=t.id)
    assert (await consent.resolve(c2, "yes, do it now")).status == "granted"


class _LoopStub:
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
