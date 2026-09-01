"""Attention classification + recovery (Prompt 1): the deterministic router picks the right category +
priority without the LLM, never destroys the primary task for a passing question, and recovery reads
durable state (not chat history)."""

from __future__ import annotations

from typing import Any

import pytest

from sali.runtime.attention import (
    AttentionCategory as C,
)
from sali.runtime.attention import (
    Priority as P,
)
from sali.runtime.attention import (
    attention_snapshot,
    classify,
    resume_context,
)
from sali.tasks.store import TaskStore


# ── pure classifier (no DB) ─────────────────────────────────────────────────────────────────────
def test_status_question_does_not_touch_the_task() -> None:
    d = classify("what are you doing right now?", has_primary=True, current_objective="build laravel app")
    assert d.category is C.CONVERSATION and not d.touches_primary
    d = classify("what did you just do?", has_primary=True)
    assert d.category is C.CONVERSATION and not d.touches_primary


def test_quick_action_keeps_primary_intact() -> None:
    d = classify("zip the file project.zip and send it here", has_primary=True, current_objective="build a site")
    assert d.category is C.QUICK_ACTION and not d.touches_primary


def test_interrupt_suspends_and_is_high_priority() -> None:
    d = classify("stop for a moment and check why nginx is down", has_primary=True, current_objective="build a site")
    assert d.category is C.INTERRUPT_TASK and d.touches_primary
    assert d.priority in (P.HIGH, P.URGENT)
    d = classify("urgent: send me the current log file now", has_primary=True)
    assert d.category is C.INTERRUPT_TASK and d.priority is P.URGENT


def test_explicit_replace_and_cancel() -> None:
    d = classify("new task: build me a flask api instead", has_primary=True, current_objective="build a laravel site")
    assert d.category is C.REPLACE_PRIMARY_TASK and d.touches_primary
    d = classify("cancel that task", has_primary=True)
    assert d.category is C.CANCEL_PRIMARY_TASK and d.touches_primary


def test_queue_for_later() -> None:
    d = classify("after you finish this website, remind me to deploy it", has_primary=True,
                 current_objective="build a website")
    assert d.category is C.QUEUE_FOR_LATER


def test_continuation_stays_on_task() -> None:
    d = classify("also make the login page use the new theme", has_primary=True,
                 current_objective="build the login page")
    assert d.category is C.CONTINUE_PRIMARY and not d.touches_primary


def test_priority_signals() -> None:
    assert classify("do it").priority is P.NORMAL
    assert classify("i need this now", has_primary=True).priority in (P.HIGH, P.URGENT)
    assert classify("whenever you have time, tidy the readme").priority is P.LOW
    assert classify("urgent, the server is down").priority is P.URGENT


def test_no_active_task_routing() -> None:
    assert classify("what's my cpu usage?").category is C.QUICK_ANSWER  # a bare question → answer
    assert classify("build me a flask api").category is C.CONTINUE_PRIMARY  # substantive → becomes the focus


# ── recovery + resume context (durable state) ───────────────────────────────────────────────────
pytestmark_db = pytest.mark.db


@pytest.mark.db
async def test_snapshot_reports_suspended_primary(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Build the Laravel app", ["scaffold", "auth", "deploy"])
    await store.activate(task.id)
    # nothing suspended yet
    snap = await attention_snapshot(live_pool)
    assert snap["has_primary"] and not snap["primary_suspended"] and not snap["should_resume_primary"]
    # suspend for an interrupt → snapshot now says resume it
    await store.suspend(task.id, reason="check nginx")
    snap = await attention_snapshot(live_pool)
    assert snap["primary_suspended"] and snap["interrupt_active"] and snap["should_resume_primary"]
    assert snap["primary_task_id"] == str(task.id)


@pytest.mark.db
async def test_resume_context_is_built_from_durable_state(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("Build the Laravel app", ["scaffold app", "add auth", "deploy"])
    await store.activate(task.id)
    await store.advance(task.id, 1, "done")
    await store.suspend(task.id, reason="quick nginx check")
    ctx = await resume_context(live_pool, task.id)
    assert ctx is not None
    assert "Build the Laravel app" in ctx
    assert "COMPLETED STEPS" in ctx and "scaffold app" in ctx
    assert "NEXT ACTION" in ctx and "add auth" in ctx  # resumes at the next step, not the start
    assert "INTERRUPT THAT OCCURRED" in ctx and "nginx" in ctx
    assert "do not restart" in ctx.lower()
