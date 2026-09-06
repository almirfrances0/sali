"""Sali skills production architecture — tests.

Covers the pieces the audit-turn added:
* Layered frontmatter parsing (summary, sections, dependencies, conflicts, version_hint,
  project_detect).
* SkillComposer: project-awareness (composer.json / package.json / pyproject.toml boosts),
  primary vs supporting split, dependency expansion, conflict elimination.
* Rendering: summary bounded, deep sections opt-in.
* Proficiency: classification thresholds + evidence-based aggregation.

These are pure/deterministic tests — no live DB, no live daemon. The proficiency + rendering
integration tests that need a real DB live in the existing tests/ layout under live_pool
fixtures (added later).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sali.skills.composer import SkillComposer, render_deep, render_summary
from sali.skills.discovery import SkillFile, discover_skills, parse_frontmatter
from sali.skills.proficiency import _classify
from sali.skills.selector import select_skills


# ── Discovery / layered frontmatter ───────────────────────────────────────────────────────────────


def test_frontmatter_parses_summary_and_all_list_keys() -> None:
    """New frontmatter keys (summary, dependencies, conflicts, version_hint, project_detect)
    all round-trip through parse_frontmatter."""
    text = (
        "---\n"
        "name: Test Skill\n"
        "tags: [alpha, beta]\n"
        "summary: One line.\n"
        "version_hint: v1\n"
        "dependencies:\n"
        "  - foo\n"
        "  - bar\n"
        "conflicts: [baz]\n"
        "project_detect:\n"
        "  - package.json\n"
        "---\n"
        "# Body\n"
        "Body text here.\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "Test Skill"
    assert meta["summary"] == "One line."
    assert meta["version_hint"] == "v1"
    assert meta["tags"] == ["alpha", "beta"]
    assert meta["dependencies"] == ["foo", "bar"]
    assert meta["conflicts"] == ["baz"]
    assert meta["project_detect"] == ["package.json"]
    assert "Body" in body


def test_discovery_splits_sections_on_h2(tmp_path: Path) -> None:
    """A skill file with H2 sections should expose each section body indexed by title."""
    (tmp_path / "demo.md").write_text(
        "---\nname: Demo\ntags: [demo]\n---\n"
        "# Demo\nPreamble text.\n\n## Setup\nSetup body.\n\n## Verification\nVerify body.\n"
    )
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "demo"
    assert "Setup" in s.sections
    assert "Verification" in s.sections
    assert "Setup body" in s.sections["Setup"]
    assert "Verify body" in s.sections["Verification"]
    # Preamble (before first H2) is folded into the summary when no explicit summary is set.
    assert "Preamble" in s.summary


def test_discovery_backward_compatible_no_frontmatter(tmp_path: Path) -> None:
    """A bare .md file (no frontmatter, no H2) still loads with sensible defaults."""
    (tmp_path / "plain.md").write_text("# Plain Skill\nJust some body content.")
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "plain"
    assert s.tags == ["plain"]   # derived from filename when frontmatter absent
    assert s.sections == {}       # no H2s
    assert "Just some body content" in s.summary


# ── Composer / project awareness ──────────────────────────────────────────────────────────────────


def _skill(name: str, *, tags: list[str] | None = None,
           project_detect: list[str] | None = None,
           dependencies: list[str] | None = None,
           conflicts: list[str] | None = None,
           summary: str = "") -> SkillFile:
    return SkillFile(name=name, title=name.title(), path=f"/tmp/{name}.md",
                     tags=tags or [name], content="body", content_hash="h" + name,
                     summary=summary or f"{name} summary", sections={},
                     dependencies=dependencies or [], conflicts=conflicts or [],
                     version_hint="", project_detect=project_detect or [])


def test_composer_boosts_skills_whose_project_detect_files_exist(tmp_path: Path) -> None:
    """A project with tailwind.config.js should promote the tailwind skill even if the objective
    didn't mention it — that's the whole point of project-awareness."""
    (tmp_path / "tailwind.config.js").write_text("module.exports = {}")
    skills = [
        _skill("tailwind", project_detect=["tailwind.config.js"]),
        _skill("random"),
    ]
    plan = SkillComposer().compose(
        skills, objective="build a login screen", message="", project_root=tmp_path)
    names = plan.names
    assert "tailwind" in names, f"expected tailwind to be selected via project_detect, got {names}"
    # Selected via project_detect alone (no tag hit from objective) — should be SUPPORTING or
    # primary depending on threshold. Just assert it made the cut with a project_detect reason.
    tailwind = next(p for p in plan.picks if p.skill.name == "tailwind")
    assert "detected in project" in tailwind.reason or "relevance" in tailwind.reason
    assert tailwind.score >= 2.0   # got the project_detect boost


def test_composer_detects_laravel_version_from_composer_json(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(json.dumps({
        "require": {"php": "^8.2", "laravel/framework": "^11.20"}
    }))
    skills = [_skill("laravel", tags=["laravel"], project_detect=["composer.json"])]
    plan = SkillComposer().compose(skills, objective="build a controller", project_root=tmp_path)
    laravel = next(p for p in plan.picks if p.skill.name == "laravel")
    assert laravel.detected_version.startswith("laravel@")
    assert "11" in laravel.detected_version
    # And a project note is surfaced so the caller can render "Laravel 11 project"
    assert any("Laravel" in n for n in plan.project_notes)


def test_composer_pulls_declared_dependencies_of_primary_picks() -> None:
    """A primary Laravel pick should pull in PHP even if PHP wasn't in the objective."""
    skills = [
        _skill("laravel", tags=["laravel"], dependencies=["php"]),
        _skill("php", tags=["php"]),
        _skill("noise", tags=["noise"]),
    ]
    plan = SkillComposer().compose(skills, objective="laravel laravel laravel", project_root=None)
    names = plan.names
    assert "laravel" in names
    assert "php" in names
    # PHP arrived via the dependency path — not because it matched the objective
    php_pick = next(p for p in plan.picks if p.skill.name == "php")
    assert php_pick.kind == "dependency"


def test_composer_drops_conflicts_of_stronger_primary() -> None:
    """A skill declared as a CONFLICT of a stronger primary should not appear (even if its own
    score would otherwise qualify)."""
    skills = [
        _skill("tailwind", tags=["tailwind", "css"], conflicts=["oldcss"]),
        _skill("oldcss", tags=["oldcss", "css"]),
    ]
    plan = SkillComposer().compose(skills, objective="tailwind tailwind css", project_root=None)
    assert "tailwind" in plan.names
    assert "oldcss" not in plan.names


def test_composer_primary_vs_supporting_split() -> None:
    """Skills scoring above the primary threshold are 'primary'; those below are 'supporting'."""
    skills = [
        _skill("laravel", tags=["laravel"]),
        _skill("php", tags=["php"]),
        _skill("weakhit", tags=["css"]),   # matches "css" once — a weak signal
    ]
    plan = SkillComposer().compose(
        skills, objective="build laravel with a hint of css", project_root=None)
    laravel = next(p for p in plan.picks if p.skill.name == "laravel")
    assert laravel.kind == "primary"   # objective + name hit
    if any(p.skill.name == "weakhit" for p in plan.picks):
        weak = next(p for p in plan.picks if p.skill.name == "weakhit")
        assert weak.kind in ("supporting", "dependency"), (
            f"a skill with only one weak tag hit should NOT be primary; got {weak.kind}")


# ── Rendering ─────────────────────────────────────────────────────────────────────────────────────


def test_render_summary_is_bounded() -> None:
    """The summary block per skill should be capped so the prefix cache stays warm."""
    skills = [_skill("bigskill", summary="x" * 5000)]
    from sali.skills.composer import SkillPick, SkillPlan
    plan = SkillPlan(picks=[SkillPick(skill=skills[0], score=5.0, kind="primary",
                                       reason="test")])
    out = render_summary(plan, per_skill_chars=800)
    # Every element under the cap plus overhead — a few thousand chars total, not the raw 5000+.
    assert len(out) < 2000, f"summary should be bounded, was {len(out)} chars"


def test_render_deep_appends_requested_sections_only() -> None:
    """render_deep should add the requested section bodies from primary picks; unrequested
    sections stay hidden (that's the whole layered-knowledge point)."""
    skill = SkillFile(name="s", title="S", path="/tmp/s.md", tags=["s"], content="",
                      content_hash="h", summary="sum", sections={
                          "Setup": "SETUP_BODY_UNIQUE_KEY",
                          "Advanced": "ADVANCED_BODY_UNIQUE_KEY",
                      })
    from sali.skills.composer import SkillPick, SkillPlan
    plan = SkillPlan(picks=[SkillPick(skill=skill, score=5.0, kind="primary", reason="test")])
    out = render_deep(plan, section_titles=["Setup"])
    assert "SETUP_BODY_UNIQUE_KEY" in out
    assert "ADVANCED_BODY_UNIQUE_KEY" not in out


# ── Proficiency classification ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("passed,failed,expected_level", [
    (0, 0, "novice"),
    (1, 0, "novice"),           # only one pass isn't enough
    (2, 0, "developing"),
    (5, 1, "competent"),
    (10, 1, "advanced"),
    (14, 1, "expert"),          # >= 12 total + 90%+ rate
    (3, 5, "developing"),       # too many failures — never advanced
    (1, 10, "developing"),      # >= 2 total gets "developing" regardless of rate
])
def test_proficiency_classification(passed: int, failed: int, expected_level: str) -> None:
    level, _ = _classify(passed, failed)
    assert level == expected_level, (
        f"passed={passed} failed={failed} → level={level} (expected {expected_level})")


def test_proficiency_confidence_rises_with_volume() -> None:
    _, low = _classify(1, 0)
    _, high = _classify(20, 0)
    assert high > low, f"confidence should rise with more evidence ({low} → {high})"
