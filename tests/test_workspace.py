"""Workspace authority (Prompt 5 §1-5) — a task always has an authoritative, durable workspace resolved
by strict priority, never /home directly, never re-resolved on continuation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sali.events.publisher import EventPublisher
from sali.tasks.store import TaskStore
from sali.tasks.workspace import (
    WorkspaceResolution,
    is_safe_workspace_root,
    resolve_task_workspace,
)

pytestmark = pytest.mark.db


def _resolve(**kw: Any) -> WorkspaceResolution:
    with tempfile.TemporaryDirectory() as works:
        kw.setdefault("sali_works_root", works)
        kw.setdefault("task_id", uuid4())
        kw.setdefault("explicit", None)
        kw.setdefault("active_task_workspace", None)
        return resolve_task_workspace(**kw)


# ── resolver priority (§4) ──────────────────────────────────────────────────────────────────────────

def test_explicit_workspace_wins() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = str(Path(d) / "portfolio")
        r = resolve_task_workspace(
            objective="build a site", explicit=target, active_task_workspace=None,
            sali_works_root=d, task_id=uuid4())
        assert str(r.workspace.workspace_root) == target and r.mode == "explicit"
        assert Path(target).is_dir()  # created on disk


def test_existing_task_workspace_wins_on_continuation() -> None:
    # priority 1: an existing task workspace is NEVER re-resolved, even if an explicit one is offered.
    r = _resolve(objective="continue the build", explicit="/tmp/somewhere-else",
                 active_task_workspace="/home/almir/projects/myapp")
    assert str(r.workspace.workspace_root) == "/home/almir/projects/myapp" and r.mode == "inherited"


def test_referenced_project_path_in_objective() -> None:
    r = _resolve(objective="Continue working on the project at /home/almir/projects/myapp")
    assert str(r.workspace.workspace_root) == "/home/almir/projects/myapp" and r.mode == "explicit"


def test_new_task_defaults_under_sali_works() -> None:
    tid = uuid4()
    with tempfile.TemporaryDirectory() as works:
        r = resolve_task_workspace(objective="Build me a Laravel portfolio website", explicit=None,
                                   active_task_workspace=None, sali_works_root=works, task_id=tid)
        assert r.mode == "auto"
        assert str(r.workspace.workspace_root) == str(Path(works) / "tasks" / str(tid))
        assert r.workspace.workspace_root.is_dir()


# ── escape protection (§3) ──────────────────────────────────────────────────────────────────────────

def test_unsafe_roots_are_rejected() -> None:
    assert not is_safe_workspace_root(Path("/"))
    assert not is_safe_workspace_root(Path("/home"))
    assert not is_safe_workspace_root(Path.home())
    assert not is_safe_workspace_root(Path("/etc"))
    assert not is_safe_workspace_root(Path("/usr"))
    assert not is_safe_workspace_root(Path("/var"))
    # subdirectories of home / a real project are fine
    assert is_safe_workspace_root(Path.home() / "projects" / "myapp")
    assert is_safe_workspace_root(Path("/home/almir/Desktop/sali-works/portfolio"))


def test_escape_falls_back_to_auto_and_reports_rejection() -> None:
    tid = uuid4()
    with tempfile.TemporaryDirectory() as works:
        r = resolve_task_workspace(objective="work here", explicit="/home", active_task_workspace=None,
                                   sali_works_root=works, task_id=tid)
        assert r.mode == "auto" and r.rejected == "/home"
        assert str(r.workspace.workspace_root) == str(Path(works) / "tasks" / str(tid))


def test_explicit_external_project_dir_still_allowed() -> None:
    # §28.8: an explicit user-targeted project directory (a home SUBDIR) is honoured, not blocked.
    with tempfile.TemporaryDirectory() as d:
        proj = str(Path(d) / "myapp")
        r = resolve_task_workspace(objective="work on my app", explicit=proj,
                                   active_task_workspace=None, sali_works_root=d, task_id=uuid4())
        assert str(r.workspace.workspace_root) == proj and r.mode == "explicit"


# ── durability: bind_workspace persists, survives, emits (§1/§4/§26) ─────────────────────────────────

async def test_bind_workspace_persists_and_survives(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("Build the app", ["scaffold"])
    with tempfile.TemporaryDirectory() as works:
        bound = await store.bind_workspace(
            task.id, objective="Build the app", explicit=None, sali_works_root=works)
        assert bound["mode"] == "auto"
        # durable: a fresh read (as after restart) returns the SAME workspace, never re-resolved
        got = await store.get(task.id)
        assert got is not None and got.workspace_root == bound["workspace_root"]
        # binding again honours the existing workspace (never re-resolves) — continuation invariant
        again = await store.bind_workspace(
            task.id, objective="Build the app", explicit="/tmp/other", sali_works_root=works)
        assert again["workspace_root"] == bound["workspace_root"] and again["mode"] == "inherited"
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='workspace.resolved' AND subject_id=$1", task.id)
    assert n >= 1


async def test_bind_workspace_rejects_unsafe_and_emits(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("Build the app", ["scaffold"])
    with tempfile.TemporaryDirectory() as works:
        bound = await store.bind_workspace(
            task.id, objective="x", explicit="/etc", sali_works_root=works)
    assert bound["mode"] == "auto" and bound["rejected"] == "/etc"
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='workspace.rejected' AND subject_id=$1", task.id)
    assert n >= 1
