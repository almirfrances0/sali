"""Prompt 5 capstone — one logical task keeps its workspace, skill snapshot, and research history through
compaction, interruption, restart, and reviewer completion, all re-derived from durable state; and those
three blocks actually reach the model on a continuation turn (not from conversational memory)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.events.publisher import EventPublisher
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.skills.store import SkillStore
from sali.tasks.research import ResearchStore
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.store import TaskStore
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


async def test_capstone_workspace_skill_research_survive_interrupt_and_restart(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub, TaskReviewer(live_pool, pub))
    skills = SkillStore(live_pool, pub)
    research = ResearchStore(live_pool, pub)

    # create the Laravel task; workspace auto-resolves under sali-works; skills selected; research done
    task = await store.create("Build a Laravel portfolio website with Tailwind",
                              ["scaffold", "auth", "deploy"])
    await store.activate(task.id)
    original_id = task.id
    bound = await store.bind_workspace(
        task.id, objective="Build a Laravel portfolio website with Tailwind", explicit=None,
        sali_works_root="/home/almir/Desktop/sali-works")
    assert bound["mode"] == "auto" and "sali-works/tasks/" in bound["workspace_root"]
    persisted = await skills.select_and_persist(
        task.id, objective="Build a Laravel portfolio website with Tailwind",
        skills_root=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "skills"))
    assert "laravel" in {r["name"] for r in persisted}
    rid = await research.record_research(
        task_id=task.id, run_id=uuid4(), step_seq=2, query="Laravel 13 Breeze auth install",
        source="https://laravel.com/docs/breeze", summary="composer require laravel/breeze --dev",
        confidence=0.8, content_hash="h1")
    cand = await research.record_candidate(
        task_id=task.id, run_id=None, lesson="Breeze installs auth for Laravel", research_id=rid)

    # INTERRUPT: suspend the primary (a quick action would run here), then resume — same task_id
    await store.suspend(task.id, reason="quick question")
    resumed = await store.resume(task.id)
    assert resumed is not None and resumed.id == original_id and resumed.status == "running"

    # PROCESS RESTART: brand-new stores (nothing from memory) re-read all durable state
    s2 = TaskStore(live_pool, pub, TaskReviewer(live_pool, pub))  # reviewer wired, as in production
    sk2, r2 = SkillStore(live_pool), ResearchStore(live_pool)
    t2 = await s2.get(original_id)
    assert t2 is not None and t2.id == original_id                 # ONE task_id
    assert t2.workspace_root == bound["workspace_root"]            # workspace restored
    assert "laravel" in {r["name"] for r in await sk2.for_task(original_id)}  # skill snapshot restored
    assert (await r2.list_research(original_id))[0]["id"] == rid   # research restored

    # work continues to completion; the reviewer PASS verifies the candidate; ONE task_id throughout
    ex = await s2.record_execution(original_id, 1, "composer", tool_args={})
    await s2.complete_execution(ex, status="completed")
    for seq in (1, 2, 3):
        await s2.advance(original_id, seq, "done", verified_by=uuid4())
    assert await s2.get(original_id) is None                       # completed + archived
    # the candidate became verified (reviewer-backed) and is now promotable to durable memory
    assert any(c["id"] == cand for c in await ResearchStore(live_pool).promotable_candidates())


async def test_workspace_skill_and_research_reach_the_model(live_pool: Any) -> None:
    """The three durable blocks are re-derived from PostgreSQL and injected into the model's context on a
    continuation turn — never depending on the model remembering them (§5/§13)."""
    fake = FakeModelProvider(responses=[ChatResult("Continuing the build.", None, [], 3, 3, "fake")])
    loop = AgentLoop(
        pool=live_pool, provider=fake, retrieval=RetrievalService(live_pool, fake),
        context=ContextEngine(fake, ctx_tokens=8192), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")))
    pub = EventPublisher(live_pool)
    loop._skills._publisher = pub
    loop._research_store._publisher = pub

    store = loop._tasks
    task = await store.create("Build a Laravel portfolio with Tailwind", ["scaffold"])
    await store.activate(task.id)
    bound = await store.bind_workspace(
        task.id, objective="Build a Laravel portfolio with Tailwind", explicit=None,
        sali_works_root="/home/almir/Desktop/sali-works")
    await loop._research_store.record_research(
        task_id=task.id, run_id=None, step_seq=1, query="Tailwind content globs",
        source="https://tailwindcss.com", summary="set content globs to your blade templates",
        confidence=0.7, content_hash="h")

    await loop.run("keep going on the Laravel portfolio")
    system = "\n".join(m.content for m in fake.calls[0]["messages"] if m.role == "system")
    assert "CURRENT TASK WORKSPACE" in system and bound["workspace_root"] in system  # workspace block
    assert "RELEVANT SKILLS" in system and "laravel" in system                       # skill guidance
    assert "RESEARCH FINDINGS" in system and "content globs" in system               # research findings


async def test_no_new_task_id_across_workspace_skill_research(live_pool: Any) -> None:
    # §28.24: none of workspace binding / skill selection / research create a second task.
    store = TaskStore(live_pool)
    task = await store.create("Build a Laravel app", ["a"])
    await store.activate(task.id)
    await store.bind_workspace(task.id, objective="Build a Laravel app", explicit=None,
                               sali_works_root="/home/almir/Desktop/sali-works")
    await SkillStore(live_pool).select_and_persist(
        task.id, objective="Build a Laravel app",
        skills_root=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "skills"))
    await ResearchStore(live_pool).record_research(
        task_id=task.id, run_id=None, step_seq=None, query="q", source="s", summary="a",
        confidence=0.5, content_hash="h")
    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM task") == 1  # still exactly one task
