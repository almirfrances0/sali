"""Sali's skills system (Prompt 5) — human-editable Markdown guidance discovered from the filesystem,
selected deterministically per task, and snapshotted durably so a task's guidance is reproducible even
if a skill file changes mid-task. Skills are task guidance; they never override system safety or policy.
"""

from __future__ import annotations

from sali.skills.discovery import SkillFile, discover_skills, parse_frontmatter
from sali.skills.selector import select_skills
from sali.skills.store import SkillStore

__all__ = ["SkillFile", "SkillStore", "discover_skills", "parse_frontmatter", "select_skills"]
