"""Completion must never delete the work it just finished.

Reproduces the real loss on task 9a2b4eb8 (2026-09-07): an `auto` workspace is `<sali-works>/tasks/
<task_id>`, which is byte-for-byte the directory `_task_dir()` returns — so `_preserve_artifacts`
copied the artifacts into `<workspace>/artifacts/` and `cleanup_task` then rmtree'd `<workspace>`,
taking the copies with it. Both `task_artifact` rows for that task point at paths that no longer
exist, and 95 of 430 task directories are empty husks.

The website survived only because Sali built it OUTSIDE the task workspace, in `sali-works/nexus-site/`.
Had he followed his own plan — which named the task workspace — completing the task would have deleted
the deliverable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from sali.tasks.cleanup import WorkspaceCleanupStore


def _ws(tmp_path, task_id):
    """A directory shaped exactly like a real ephemeral task workspace."""
    root = tmp_path / "sali-works" / "tasks" / str(task_id)
    root.mkdir(parents=True)
    return root


async def _task(conn, task_id, root, *, mode="auto"):
    await conn.execute(
        "INSERT INTO task (id, objective, status, workspace_root, workspace_mode) "
        "VALUES ($1,'build the nexus site','done',$2,$3)", task_id, str(root), mode)


@pytest.mark.asyncio
async def test_unpreserved_output_vetoes_the_cleanup(live_pool, tmp_path) -> None:
    """THE regression. Files the task produced but never registered have no copy anywhere, so the
    only safe thing to do with them is nothing."""
    task_id = uuid4()
    root = _ws(tmp_path, task_id)
    (root / "index.html").write_text("<!DOCTYPE html><title>Nexus</title>")
    (root / "pricing.html").write_text("<!DOCTYPE html><title>Pricing</title>")
    async with live_pool.acquire() as conn:
        await _task(conn, task_id, root)

    res = await WorkspaceCleanupStore(live_pool).cleanup_task(task_id)

    assert res["status"] == "skipped", "a workspace holding unpreserved output must not be deleted"
    assert (root / "index.html").is_file(), "the deliverable must still be on disk"
    assert (root / "pricing.html").is_file()
    assert "no preserved copy" in res["reason"]


@pytest.mark.asyncio
async def test_the_archive_survives_cleanup(live_pool, tmp_path) -> None:
    """When everything IS preserved, the working tree goes but the archive stays — it lives in the
    same directory and has a different lifetime. `/tasks/history` reads meta.json from right here."""
    task_id = uuid4()
    root = _ws(tmp_path, task_id)
    # the archive `_preserve_artifacts` + `save_task_record` wrote
    (root / "artifacts").mkdir()
    (root / "artifacts" / f"{uuid4()}_contact.html").write_text("<!DOCTYPE html>")
    (root / "meta.json").write_text('{"objective":"build the nexus site"}')
    (root / "events.jsonl").write_text('{"event":"tool_executed"}\n')
    # the working copy, which IS registered and therefore has a preserved twin
    (root / "contact.html").write_text("<!DOCTYPE html>")
    (root / "scratch").mkdir()
    (root / "scratch" / "contact.html").write_text("draft")

    async with live_pool.acquire() as conn:
        await _task(conn, task_id, root)
        await conn.execute(
            "INSERT INTO task_artifact (task_id, artifact_path, artifact_type, tool_name) "
            "VALUES ($1,$2,'created','create_file')", task_id, str(root / "contact.html"))

    res = await WorkspaceCleanupStore(live_pool).cleanup_task(task_id)

    assert res["status"] == "completed"
    assert (root / "meta.json").is_file(), "the task snapshot is the durable record"
    assert (root / "events.jsonl").is_file()
    assert list((root / "artifacts").iterdir()), "the preserved artifact copies must survive"
    assert not (root / "contact.html").exists(), "the working copy is reclaimed (it has a copy)"
    assert not (root / "scratch").exists()


@pytest.mark.asyncio
async def test_a_user_owned_workspace_is_never_touched(live_pool, tmp_path) -> None:
    task_id = uuid4()
    root = _ws(tmp_path, task_id)
    (root / "index.html").write_text("mine")
    async with live_pool.acquire() as conn:
        await _task(conn, task_id, root, mode="explicit")

    res = await WorkspaceCleanupStore(live_pool).cleanup_task(task_id)
    assert res["status"] == "skipped"
    assert (root / "index.html").is_file()


@pytest.mark.asyncio
async def test_a_path_that_is_not_the_task_dir_is_refused(live_pool, tmp_path) -> None:
    """The pre-existing path guard still holds: only <sali-works>/tasks/<task_id> is ever eligible."""
    task_id = uuid4()
    root = tmp_path / "sali-works" / "nexus-site"      # a real deliverable folder, not a task dir
    root.mkdir(parents=True)
    (root / "index.html").write_text("the website")
    async with live_pool.acquire() as conn:
        await _task(conn, task_id, root)

    res = await WorkspaceCleanupStore(live_pool).cleanup_task(task_id)
    assert res["status"] == "skipped"
    assert (root / "index.html").is_file()


@pytest.mark.asyncio
async def test_the_resume_path_is_guarded_too(live_pool, tmp_path) -> None:
    """A cleanup that failed once comes back later. It used a blanket rmtree, so the retry would
    destroy exactly what the primary path now refuses to."""
    task_id = uuid4()
    root = _ws(tmp_path, task_id)
    (root / "index.html").write_text("<!DOCTYPE html>")
    async with live_pool.acquire() as conn:
        await _task(conn, task_id, root)
        await conn.execute(
            "INSERT INTO workspace_cleanup (task_id, workspace_root, workspace_type, policy, status) "
            "VALUES ($1,$2,'ephemeral','auto','failed')", task_id, str(root))

    await WorkspaceCleanupStore(live_pool).resume_pending()
    assert (root / "index.html").is_file(), "the retry must not delete unpreserved output either"
