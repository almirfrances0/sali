"""Sali skills — a real capability system, not a folder of markdown notes.

* `discovery.SkillFile` / `discover_skills` — load .md skills with layered frontmatter (summary,
  sections, dependencies, conflicts, version_hint, project_detect).
* `selector.select_skills` — deterministic tag-overlap scoring (the composer's first pass).
* `composer.SkillComposer` — project-aware composition (reads composer.json / package.json /
  pyproject.toml / Dockerfile from a target project root, boosts skills whose files are present,
  splits primary vs supporting, pulls in declared dependencies, drops declared conflicts).
* `composer.render_summary` / `render_deep` — layered rendering: always the summary; deep
  sections only when explicitly requested (context budget stays flat by default).
* `store.SkillStore` — per-task snapshot persistence + entry point for both task and chat paths.
* `proficiency.compute_all` — evidence-based per-skill proficiency from task_review outcomes.
"""

from sali.skills.composer import SkillComposer, SkillPick, SkillPlan, render_deep, render_summary
from sali.skills.discovery import SkillFile, discover_skills, parse_frontmatter
from sali.skills.proficiency import (
    PROFICIENCY_LEVELS,
    SkillProficiency,
    compute_all,
    compute_for,
)
from sali.skills.selector import select_skills
from sali.skills.store import SkillStore

__all__ = [
    "PROFICIENCY_LEVELS", "SkillComposer", "SkillFile", "SkillPick", "SkillPlan",
    "SkillProficiency", "SkillStore", "compute_all", "compute_for", "discover_skills",
    "parse_frontmatter", "render_deep", "render_summary", "select_skills",
]
