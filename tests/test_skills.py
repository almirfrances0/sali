"""Skills system (Prompt 5 §6-13) — human-editable Markdown, deterministic selection, durable per-task
snapshots that survive compaction/restart and don't silently corrupt an active task when the file changes."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from sali.events.publisher import EventPublisher
from sali.skills.discovery import discover_skills, parse_frontmatter
from sali.skills.selector import select_skills
from sali.skills.store import SkillStore
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db

_REPO_SKILLS = str(Path(__file__).resolve().parents[1] / "skills")


def _write(d: str, name: str, content: str) -> None:
    (Path(d) / name).write_text(content, encoding="utf-8")


# ── discovery + frontmatter (§6/§8) ──────────────────────────────────────────────────────────────────

def test_discovery_finds_new_markdown_files() -> None:
    with tempfile.TemporaryDirectory() as d:
        _write(d, "laravel.md", "---\nname: Laravel\ntags:\n  - laravel\n  - php\n---\n# Laravel\nbody")
        _write(d, "plain.md", "# Just Markdown\nno frontmatter here")  # bare markdown still discovered
        found = {s.name: s for s in discover_skills(Path(d))}
        assert set(found) == {"laravel", "plain"}
        assert found["laravel"].tags == ["laravel", "php"] and found["laravel"].title == "Laravel"
        assert found["plain"].tags == ["plain"]  # tags derived from the filename when none given


def test_frontmatter_parses_inline_and_block_tags() -> None:
    meta, body = parse_frontmatter("---\nname: X\ntags: [a, b, c]\n---\nhello")
    assert meta["name"] == "X" and meta["tags"] == ["a", "b", "c"] and body == "hello"
    meta2, _ = parse_frontmatter("no frontmatter at all")
    assert meta2 == {}


# ── deterministic selection (§7/§8/§11) ──────────────────────────────────────────────────────────────

def test_laravel_task_selects_laravel_and_excludes_irrelevant() -> None:
    skills = discover_skills(Path(_REPO_SKILLS))
    chosen = [s.name for s, _ in select_skills(
        skills, objective="Build a Laravel 13 portfolio website with Tailwind", limit=3)]
    assert "laravel" in chosen and "tailwind" in chosen
    # the irrelevant skills are NOT injected
    for irrelevant in ("docker", "nextjs", "postgresql", "kali-linux"):
        assert irrelevant not in chosen


def test_selection_is_empty_when_nothing_matches() -> None:
    skills = discover_skills(Path(_REPO_SKILLS))
    assert select_skills(skills, objective="write a haiku about the sea") == []


# ── durable per-task snapshots (§10/§11/§13) ─────────────────────────────────────────────────────────

async def test_select_persist_and_survive_restart(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("Build a Laravel portfolio with Tailwind", ["scaffold"])
    skills = SkillStore(live_pool, pub)
    persisted = await skills.select_and_persist(
        task.id, objective="Build a Laravel portfolio with Tailwind", skills_root=_REPO_SKILLS)
    names = {r["name"] for r in persisted}
    assert "laravel" in names and all(r["content"] for r in persisted)  # snapshots have content
    # a FRESH store (as after a process restart) reads the same durable snapshots
    reloaded = await SkillStore(live_pool).for_task(task.id)
    assert {r["name"] for r in reloaded} == names
    async with live_pool.acquire() as c:
        sel = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='skill.selected' AND subject_id=$1", task.id)
    assert sel >= 1


async def test_skill_change_does_not_corrupt_active_task(live_pool: Any) -> None:
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    task = await store.create("Do a docker build", ["build"])
    with tempfile.TemporaryDirectory() as d:
        _write(d, "docker.md", "---\nname: Docker\ntags:\n  - docker\n---\n# Docker\noriginal guidance")
        skills = SkillStore(live_pool, pub)
        persisted = await skills.select_and_persist(
            task.id, objective="Do a docker build", skills_root=d)
        assert any(r["name"] == "docker" for r in persisted)
        snapshot = next(r["content"] for r in persisted if r["name"] == "docker")
        # the live file changes MID-TASK
        _write(d, "docker.md", "---\nname: Docker\ntags:\n  - docker\n---\n# Docker\nDIFFERENT guidance")
        changed = await skills.detect_changes(task.id, d)
        assert "docker" in changed  # detected
        # …but the task still runs on the ORIGINAL snapshot — not the edited file
        stored = await skills.for_task(task.id)
        assert next(r["content"] for r in stored if r["name"] == "docker") == snapshot
    async with live_pool.acquire() as c:
        ch = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='skill.changed' AND subject_id=$1", task.id)
    assert ch >= 1


def test_render_is_bounded() -> None:
    store = SkillStore(None, per_skill_chars=100, max_skills=2)
    stored = [
        {"name": "a", "content": "x" * 5000, "path": "a", "content_hash": "h", "score": 3.0},
        {"name": "b", "content": "y" * 5000, "path": "b", "content_hash": "h", "score": 2.0},
        {"name": "c", "content": "z" * 5000, "path": "c", "content_hash": "h", "score": 1.0},
    ]
    rendered = store.render(stored)
    assert len(rendered) < 500  # 2 skills × ~100 chars + headers — bounded, never a 15k dump
    assert "## a" in rendered and "## b" in rendered and "## c" not in rendered  # capped at max_skills
