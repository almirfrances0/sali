"""Skill composition — turn an objective + optional target project into a REASONED PLAN of
primary + supporting skills, with detected versions and project notes.

The old selector returned "top-K by tag overlap" and stopped. The composer:

* Runs the same tag scorer to seed candidates (deterministic, cheap).
* Boosts skills whose `project_detect` files exist in the target project (Laravel wins big when
  `composer.json` is present; Tailwind wins big when `tailwind.config.js` is present).
* Detects the installed version of each hit skill (Laravel version from composer.json, Tailwind
  from package.json, Python from pyproject.toml, etc.) so the caller can render "the project
  uses Laravel 11 — prefer Laravel 11 idioms".
* Splits picks into PRIMARY (score >= primary threshold) and SUPPORTING (below primary but above
  the min-relevance floor).
* Expands with declared `dependencies` — if Laravel is primary, Blade + PHP get pulled in as
  supporting even if the objective didn't mention them.
* Drops any skill declared as a CONFLICT of a stronger pick.

The output is a `SkillPlan` — a dataclass the caller (SkillStore, the API endpoint, or the
diagnostics UI) can serialize, persist, or render directly. This is INTENTIONALLY separate from
snapshot persistence — the plan is regenerated each turn; the snapshot is durable per-task.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sali.skills.discovery import SkillFile
from sali.skills.selector import select_skills


# Score threshold for "primary" — a skill this relevant should carry primary weight in planning.
_PRIMARY_SCORE = 3.0
_MIN_RELEVANCE = 1.0
_PROJECT_DETECT_BOOST = 2.5


@dataclass(slots=True)
class SkillPick:
    """One skill chosen for a task, with the reason it was picked."""
    skill: SkillFile
    score: float
    kind: str                      # "primary" | "supporting" | "dependency"
    reason: str                    # short human-readable justification
    detected_version: str = ""     # inferred from the project, if inspection succeeded


@dataclass(slots=True)
class SkillPlan:
    """The composer's output for one objective."""
    picks: list[SkillPick] = field(default_factory=list)
    project_root: str = ""
    project_notes: list[str] = field(default_factory=list)   # things worth telling Sali about the project

    @property
    def names(self) -> list[str]:
        return [p.skill.name for p in self.picks]

    @property
    def primary(self) -> list[SkillPick]:
        return [p for p in self.picks if p.kind == "primary"]

    @property
    def supporting(self) -> list[SkillPick]:
        return [p for p in self.picks if p.kind != "primary"]

    def to_dict(self) -> dict[str, object]:
        return {
            "picks": [
                {"name": p.skill.name, "title": p.skill.title, "score": p.score,
                 "kind": p.kind, "reason": p.reason, "detected_version": p.detected_version,
                 "path": p.skill.path, "content_hash": p.skill.content_hash}
                for p in self.picks],
            "project_root": self.project_root,
            "project_notes": self.project_notes,
        }


# ── Project-awareness inspectors ──────────────────────────────────────────────────────────────────
#
# Each returns a version-string hint (empty if the file wasn't there or couldn't be parsed) plus
# an optional "note" string that the composer surfaces so Sali knows what shape the project takes.
# All inspectors are best-effort: a malformed file returns ("", "") not an exception. They read
# small files only and never traverse the filesystem beyond the declared paths.


def _read_small(path: Path, cap: int = 200_000) -> str:
    """Read a project file with a byte cap so we never load a huge lockfile into memory."""
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _detect_composer(root: Path) -> tuple[str, str]:
    """Laravel + PHP version from composer.json."""
    text = _read_small(root / "composer.json")
    if not text:
        return "", ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "", "composer.json present but unparseable"
    require = data.get("require") or {}
    laravel = require.get("laravel/framework") or require.get("laravel/lumen-framework") or ""
    php = require.get("php") or ""
    if laravel:
        return f"laravel@{laravel}", f"Laravel {laravel} project (composer.json). PHP constraint: {php or 'unset'}."
    if php:
        return f"php@{php}", f"PHP project (composer.json). PHP constraint: {php}."
    return "", "composer.json present but no Laravel/PHP constraint"


def _detect_package_json(root: Path) -> tuple[dict[str, str], list[str]]:
    """Return a dict of interesting JS framework versions from package.json + a list of notes."""
    text = _read_small(root / "package.json")
    if not text:
        return {}, []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}, ["package.json present but unparseable"]
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    versions: dict[str, str] = {}
    notes: list[str] = []
    for key in ("next", "react", "react-dom", "tailwindcss", "typescript", "vue", "svelte",
                "@shadcn/ui", "shadcn-ui"):
        v = deps.get(key)
        if v:
            versions[key] = v
    if "next" in versions:
        notes.append(f"Next.js {versions['next']} project (package.json).")
    if "react" in versions and "next" not in versions:
        notes.append(f"React {versions['react']} project (package.json).")
    if "tailwindcss" in versions:
        notes.append(f"Tailwind {versions['tailwindcss']} present.")
    if "typescript" in versions:
        notes.append(f"TypeScript {versions['typescript']} present.")
    if not notes and deps:
        notes.append("package.json present; no recognised framework — inspect deps manually.")
    return versions, notes


def _detect_pyproject(root: Path) -> tuple[dict[str, str], list[str]]:
    """Return Python framework versions from pyproject.toml (dependencies section)."""
    text = _read_small(root / "pyproject.toml")
    if not text:
        return {}, []
    notes: list[str] = []
    versions: dict[str, str] = {}
    # Cheap regex-based scan — a proper TOML parse isn't needed for hint extraction and avoids
    # pulling in an extra dep. Captures both PEP 631 and Poetry styles.
    for target in ("fastapi", "django", "flask", "starlette", "pydantic", "uvicorn"):
        m = re.search(rf'["\']?{target}[\s\[\]a-z-]*["\']?\s*[=~>@,]+\s*["\']?([0-9][\w.\-*]+)',
                      text, re.IGNORECASE)
        if m:
            versions[target] = m.group(1)
    py = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', text)
    if py:
        versions["python"] = py.group(1)
        notes.append(f"Python {py.group(1)} project (pyproject.toml).")
    if "fastapi" in versions:
        notes.append(f"FastAPI {versions['fastapi']} in pyproject.")
    return versions, notes


def _detect_dockerfile(root: Path) -> tuple[str, str]:
    for name in ("Dockerfile", "dockerfile", "Dockerfile.dev"):
        text = _read_small(root / name, cap=50_000)
        if text:
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip().upper().startswith("FROM ")), "")
            return "docker@present", f"Dockerfile present — base image line: {first[:120]}"
    return "", ""


def _inspect_project(root: Path | None) -> tuple[dict[str, str], list[str]]:
    """Aggregate every inspector; returns ({skill_name → detected_version}, [notes...])."""
    if root is None or not root.is_dir():
        return {}, []
    versions: dict[str, str] = {}
    notes: list[str] = []
    # composer
    ver, note = _detect_composer(root)
    if ver:
        # Map to skill slugs (`laravel` / `php`) via the version key prefix.
        if ver.startswith("laravel@"):
            versions["laravel"] = ver
            versions["php"] = versions.get("php") or ""  # PHP is implicit under Laravel
        elif ver.startswith("php@"):
            versions["php"] = ver
        notes.append(note)
    elif note:
        notes.append(note)
    # package.json
    js_versions, js_notes = _detect_package_json(root)
    for key, v in js_versions.items():
        # Map npm package name → skill slug
        skill = {"next": "nextjs", "react": "react", "react-dom": "react",
                 "tailwindcss": "tailwind", "typescript": "typescript",
                 "@shadcn/ui": "shadcn", "shadcn-ui": "shadcn"}.get(key)
        if skill:
            versions[skill] = f"{key}@{v}"
    notes.extend(js_notes)
    # pyproject.toml
    py_versions, py_notes = _detect_pyproject(root)
    for key, v in py_versions.items():
        versions[key] = f"{key}@{v}"
    notes.extend(py_notes)
    # Dockerfile
    ver, note = _detect_dockerfile(root)
    if ver:
        versions["docker"] = ver
        notes.append(note)
    return versions, notes


# ── Composer ──────────────────────────────────────────────────────────────────────────────────────


class SkillComposer:
    """Compose a set of skills for an objective, optionally aware of a target project."""

    def __init__(self, *, primary_score: float = _PRIMARY_SCORE,
                 min_relevance: float = _MIN_RELEVANCE,
                 max_picks: int = 6, max_primary: int = 3) -> None:
        self._primary_score = primary_score
        self._min_relevance = min_relevance
        self._max_picks = max_picks
        self._max_primary = max_primary

    def compose(
        self, skills: list[SkillFile], *, objective: str, message: str = "",
        project_root: str | Path | None = None,
    ) -> SkillPlan:
        # 1. deterministic tag scorer for the objective + triggering message
        scored = select_skills(skills, objective=objective, message=message,
                               limit=len(skills), min_score=0)
        by_name = {s.name: s for s in skills}
        score_by_name: dict[str, float] = {s.name: sc for s, sc in scored}

        # 2. project inspection boosts + version detection
        root = Path(project_root).expanduser() if project_root else None
        detected_versions, project_notes = _inspect_project(root)
        for skill in skills:
            hit = False
            for path in skill.project_detect:
                if root is not None and (root / path).exists():
                    hit = True
                    break
            if hit:
                score_by_name[skill.name] = score_by_name.get(skill.name, 0.0) + _PROJECT_DETECT_BOOST
            # Also: a skill whose slug matches a detected framework key gets a boost.
            if skill.name in detected_versions:
                score_by_name[skill.name] = score_by_name.get(skill.name, 0.0) + _PROJECT_DETECT_BOOST

        # 3. filter by relevance, sort by score desc + name asc
        ranked = sorted(
            [(by_name[n], sc) for n, sc in score_by_name.items() if sc >= self._min_relevance],
            key=lambda x: (-x[1], x[0].name),
        )

        # 4. split primary / supporting; enforce caps
        picks: list[SkillPick] = []
        primary_count = 0
        for skill, sc in ranked:
            if len(picks) >= self._max_picks:
                break
            if sc >= self._primary_score and primary_count < self._max_primary:
                picks.append(SkillPick(
                    skill=skill, score=round(sc, 2), kind="primary",
                    reason=_pick_reason(skill, sc, detected_versions),
                    detected_version=detected_versions.get(skill.name, "")))
                primary_count += 1
            else:
                picks.append(SkillPick(
                    skill=skill, score=round(sc, 2), kind="supporting",
                    reason=_pick_reason(skill, sc, detected_versions),
                    detected_version=detected_versions.get(skill.name, "")))

        # 5. pull in declared dependencies of primary picks that weren't already selected
        chosen_names = {p.skill.name for p in picks}
        for pick in list(picks):
            if pick.kind != "primary":
                continue
            for dep_name in pick.skill.dependencies:
                if dep_name in chosen_names:
                    continue
                dep = by_name.get(dep_name)
                if dep is None:
                    continue
                if len(picks) >= self._max_picks + 2:  # small tolerance for dependency expansion
                    break
                picks.append(SkillPick(
                    skill=dep, score=0.0, kind="dependency",
                    reason=f"pulled in as a dependency of {pick.skill.name}",
                    detected_version=detected_versions.get(dep.name, "")))
                chosen_names.add(dep_name)

        # 6. remove any pick declared as a CONFLICT of a stronger primary
        conflicts_of_strong = set()
        for pick in picks:
            if pick.kind == "primary":
                conflicts_of_strong.update(pick.skill.conflicts)
        picks = [p for p in picks if p.skill.name not in conflicts_of_strong or p.kind == "primary"]

        return SkillPlan(picks=picks, project_root=str(root or ""),
                         project_notes=project_notes)


def _pick_reason(skill: SkillFile, score: float, detected: dict[str, str]) -> str:
    parts = []
    if skill.name in detected:
        parts.append(f"detected in project ({detected[skill.name]})")
    if score >= _PRIMARY_SCORE:
        parts.append(f"strong relevance score {score:.1f}")
    elif score >= _MIN_RELEVANCE:
        parts.append(f"relevance {score:.1f}")
    if not parts:
        parts.append("candidate")
    return ", ".join(parts)


# ── Rendering ────────────────────────────────────────────────────────────────────────────────────
#
# The renderer takes a SkillPlan (or a list of stored task_skill rows in legacy shape) and returns
# the string that goes into the prompt's skills section. Two rendering modes:
#
# 1. `render_summary(plan)` — a compact block with each skill's SUMMARY only, plus one line of
#    project notes. This is what a chat turn or a light-context task gets. Bounded to keep the
#    prefix cheap.
#
# 2. `render_deep(plan, sections=[section_titles])` — the summary block PLUS the requested deep
#    sections from each primary skill. Used when the task explicitly needs the detail (e.g. "set
#    up shadcn" activates the shadcn skill's "Setup" section, not just its summary).


def render_summary(plan: SkillPlan, *, per_skill_chars: int = 800, max_skills: int = 4) -> str:
    if not plan.picks:
        return ""
    parts: list[str] = []
    if plan.project_notes:
        # First line: what shape the project is (Laravel 11, Next.js 15, etc.). This is the
        # BIGGEST leverage the composer adds — it stops the model from teaching Sali generic
        # Laravel and lets him apply Laravel-11-specific idioms.
        parts.append("PROJECT: " + " | ".join(plan.project_notes[:3])[:400])
    header = ("RELEVANT SKILLS (guidance for this task; safety and policy still win, and "
              "evidence beats what a skill claims):")
    parts.append(header)
    for pick in plan.picks[:max_skills]:
        skill = pick.skill
        summary = (skill.summary or skill.content).strip()
        if len(summary) > per_skill_chars:
            summary = summary[:per_skill_chars].rstrip() + " …"
        tag = pick.kind
        version = f" · {pick.detected_version}" if pick.detected_version else ""
        parts.append(f"\n## {skill.title} ({tag}{version})\n{summary}")
    return "\n".join(parts)


def render_deep(plan: SkillPlan, section_titles: list[str], *, per_section_chars: int = 1500,
                max_primary: int = 3) -> str:
    """Render the summary block plus specific deep sections from primary skills."""
    base = render_summary(plan)
    if not section_titles:
        return base
    wanted = {t.lower() for t in section_titles}
    detail_parts: list[str] = []
    for pick in plan.primary[:max_primary]:
        skill = pick.skill
        matched = [(t, b) for t, b in skill.sections.items() if t.lower() in wanted]
        if not matched:
            continue
        detail_parts.append(f"\n### {skill.title} — DETAIL")
        for title, body in matched[:3]:
            snippet = body.strip()
            if len(snippet) > per_section_chars:
                snippet = snippet[:per_section_chars].rstrip() + " …"
            detail_parts.append(f"\n#### {title}\n{snippet}")
    return base + "\n" + "\n".join(detail_parts) if detail_parts else base
