"""A task must not be able to call itself done when its own objective was not met.

Reproduces the two holes found on task 9a2b4eb8 (2026-09-07), which passed review 7/7 while shipping
four pages in three different design systems and an empty css/ directory:

  1. the OBJECTIVE was never a requirement — only the steps Sali wrote and the artifacts he registered
  2. a SYMLINK satisfied "create this file", which is how the deliverable ended up in two folders
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from sali.tasks.reviewer import TaskReviewer, objective_requirements
from sali.tools.probe import path_exists, path_is_real_file


# ── the objective is a requirement ───────────────────────────────────────────────────────────────

def test_the_objective_is_split_into_the_things_it_asks_for() -> None:
    reqs = objective_requirements(
        "Build a professional SaaS website for Nexus with 4 HTML pages using Tailwind CSS CDN "
        "+ shared styling")
    joined = " | ".join(reqs).lower()
    assert "shared styling" in joined, "the requirement that was actually missed must be named"
    assert "4 html pages" in joined
    assert "tailwind css cdn" in joined


def test_filler_is_not_mistaken_for_a_requirement() -> None:
    assert objective_requirements("") == []
    assert objective_requirements("the, a, and, to") == []


@pytest.mark.asyncio
async def test_an_unchecked_objective_requirement_is_named_and_counted(live_pool) -> None:
    """A task may still pass — bouncing every first review would be friction without truth — but the
    requirement nobody checked must be NAMED in the durable record, counted as unresolved, and turned
    into a recommendation. Silence is what let "7 passed" stand over a site that missed its brief."""
    task_id = uuid4()
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task (id, objective, status) VALUES ($1,$2,'running')",
            task_id, "Build a site with 4 HTML pages + shared styling")
        await conn.execute(
            "INSERT INTO task_step (task_id, seq, description, status, verified) "
            "VALUES ($1, 1, 'Create index.html', 'done', true)", task_id)

    res = await TaskReviewer(live_pool).review(task_id)

    assert res.status.value == "passed"
    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT requirements_checked, unknown_count, recommendations FROM task_review "
            "WHERE task_id=$1 ORDER BY attempt DESC LIMIT 1", task_id)
    reqs = row["requirements_checked"]
    named = " ".join(str(r.get("requirement", "")) for r in reqs).lower()
    assert "shared styling" in named, "the missed requirement must be visible in the record"
    assert row["unknown_count"] >= 1
    assert "shared styling" in str(row["recommendations"]).lower()


@pytest.mark.asyncio
async def test_the_unresolved_count_is_not_folded_into_passed(live_pool) -> None:
    """The old summary read "7 passed, 0 failed, 0 unresolved" for a task that missed its brief. The
    unresolved requirements must survive into the counts on every attempt, not just the first."""
    task_id = uuid4()
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task (id, objective, status) VALUES ($1,$2,'running')",
            task_id, "Build a site with 4 HTML pages + shared styling")
        await conn.execute(
            "INSERT INTO task_step (task_id, seq, description, status, verified) "
            "VALUES ($1, 1, 'Create index.html', 'done', true)", task_id)

    reviewer = TaskReviewer(live_pool)
    await reviewer.review(task_id)
    await reviewer.review(task_id)

    async with live_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT unknown_count, summary FROM task_review WHERE task_id=$1 ORDER BY attempt", task_id)
    assert all(r["unknown_count"] >= 1 for r in rows), "the gap must not fade on a retry"
    assert "unresolved" in rows[-1]["summary"]
    assert not rows[-1]["summary"].startswith("0 passed, 0 failed, 0 unresolved")


@pytest.mark.asyncio
async def test_a_requirement_a_step_already_names_is_not_duplicated(live_pool) -> None:
    task_id = uuid4()
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task (id, objective, status) VALUES ($1,$2,'running')",
            task_id, "Create the report, shared styling")
        await conn.execute(
            "INSERT INTO task_step (task_id, seq, description, status, verified) "
            "VALUES ($1, 1, 'apply shared styling to every page', 'done', true)", task_id)

    await TaskReviewer(live_pool).review(task_id)
    async with live_pool.acquire() as conn:
        reqs = (await conn.fetchrow(
            "SELECT requirements_checked FROM task_review WHERE task_id=$1", task_id))["requirements_checked"]
    objective_rows = [r for r in reqs if r.get("kind") == "objective"]
    assert not any("shared styling" in str(r["requirement"]).lower() for r in objective_rows), \
        "a step that names the requirement already speaks for it"


# ── a symlink is not a file you created ──────────────────────────────────────────────────────────

def test_a_symlink_does_not_count_as_a_created_file(tmp_path) -> None:
    real = tmp_path / "nexus-site"
    real.mkdir()
    (real / "index.html").write_text("<!DOCTYPE html>")
    linked = tmp_path / "nexus"
    linked.mkdir()
    os.symlink(real / "index.html", linked / "index.html")

    # "is there something there?" — yes, and that answer is still correct.
    assert path_exists(str(linked / "index.html")).success is True
    # "did you create this file here?" — no.
    v = path_is_real_file(str(linked / "index.html"))
    assert v.success is False
    assert "symlink" in v.detail

    assert path_is_real_file(str(real / "index.html")).success is True
    assert path_is_real_file(str(tmp_path / "nope.html")).success is False


def test_the_step_gate_rejects_a_symlink(tmp_path) -> None:
    """Sali was refused four times, then made the refusal go away with a symlink instead of by
    moving the work. The gate now says what is actually wrong."""
    from sali.tools.builtins.task_tool import _verify_definition_of_done

    real = tmp_path / "nexus-site"
    real.mkdir()
    (real / "index.html").write_text("<!DOCTYPE html>")
    linked = tmp_path / "nexus"
    linked.mkdir()
    os.symlink(real / "index.html", linked / "index.html")

    class _Step:
        seq = 1
        definition_of_done = f"{linked / 'index.html'} exists"

    class _Task:
        steps = [_Step()]
        workspace_root = str(tmp_path)
        allowed_write_roots: list[str] = []

    ok, reason = _verify_definition_of_done(_Task(), 1)
    assert ok is False
    assert "symlink" in reason

    class _Step2(_Step):
        definition_of_done = f"{real / 'index.html'} exists"

    class _Task2(_Task):
        steps = [_Step2()]

    assert _verify_definition_of_done(_Task2(), 1)[0] is True
