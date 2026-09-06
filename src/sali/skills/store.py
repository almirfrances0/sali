"""SkillStore — durable per-task skill snapshots + skill-composition entry point.

Two responsibilities:

1. **Snapshot persistence** (Prompt 5 §10/§11, unchanged contract): the SNAPSHOT of every selected
   skill (content + hash + composer metadata) is written to `sali.task_skill` so the task remains
   reproducible across compaction / interruption / restart even if the live `.md` is edited
   mid-task. Change detection on the live file emits `skill.changed` but the task keeps running
   on its original snapshot.

2. **Composition** (production upgrade): `select_and_persist` now runs the SkillComposer, which is
   PROJECT-AWARE (reads composer.json/package.json/pyproject.toml/Dockerfile from the workspace),
   VERSION-AWARE (each pick can carry a detected version), and returns primary + supporting +
   dependency picks — not just top-K by tag overlap.

For lightweight chat turns (no active task), see `compose_for_chat` — same composer, no
persistence, no side-effects.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger
from sali.skills.composer import SkillComposer, SkillPlan, render_deep, render_summary
from sali.skills.discovery import discover_skills

log = get_logger("sali.skills.store")


class SkillStore:
    def __init__(
        self, pool: Any, publisher: Any = None, *,
        per_skill_chars: int = 1200, max_skills: int = 4,
    ) -> None:
        self._pool = pool
        self._publisher = publisher
        self._per_skill_chars = per_skill_chars
        self._max_skills = max_skills
        self._composer = SkillComposer(max_picks=max_skills)

    async def for_task(self, task_id: UUID) -> list[dict[str, Any]]:
        """The durable skill snapshots selected for a task."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, path, content_hash, content, score, kind, reason, detected_version "
                "FROM task_skill WHERE task_id = $1 ORDER BY "
                "  CASE kind WHEN 'primary' THEN 0 WHEN 'supporting' THEN 1 ELSE 2 END, "
                "  score DESC, name", task_id)
        return [dict(r) for r in rows]

    async def select_and_persist(
        self, task_id: UUID, *, objective: str, skills_root: str, message: str = "",
        project_root: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compose skills for this objective (project-aware where possible), persist their
        snapshots + composer metadata, and return the stored rows. Idempotent — re-running for a
        task that already has skills is a no-op insert per (task_id, name).
        """
        skills = discover_skills(Path(skills_root))
        plan = self._composer.compose(
            skills, objective=objective, message=message, project_root=project_root)
        async with self._pool.acquire() as conn:
            for pick in plan.picks:
                s = pick.skill
                await conn.execute(
                    "INSERT INTO task_skill "
                    "(task_id, name, path, content_hash, content, score, kind, reason, detected_version) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (task_id, name) DO NOTHING",
                    task_id, s.name, s.path, s.content_hash, s.content, pick.score,
                    pick.kind, pick.reason, pick.detected_version)
        await self._emit("skill.discovered", task_id,
                         {"available": len(skills), "selected": plan.names,
                          "project_notes": plan.project_notes})
        for pick in plan.picks:
            await self._emit("skill.selected", task_id, {
                "name": pick.skill.name, "path": pick.skill.path, "score": pick.score,
                "kind": pick.kind, "reason": pick.reason,
                "detected_version": pick.detected_version,
                "content_hash": pick.skill.content_hash})
        return await self.for_task(task_id)

    def compose_for_chat(
        self, *, objective: str, message: str = "", skills_root: str,
        project_root: str | None = None,
    ) -> SkillPlan:
        """Compose skills for a lightweight chat turn — no snapshot, no persistence, no events.
        The plan is regenerated each turn (cheap; composer is deterministic + pure)."""
        skills = discover_skills(Path(skills_root))
        return self._composer.compose(
            skills, objective=objective, message=message, project_root=project_root)

    async def detect_changes(self, task_id: UUID, skills_root: str) -> list[str]:
        """Compare live skill files to the task's snapshots; emit skill.changed for any that differ.
        The snapshot is KEPT — the task continues on the original guidance (durable reproducibility)."""
        stored = await self.for_task(task_id)
        if not stored:
            return []
        live = {s.name: s for s in discover_skills(Path(skills_root))}
        changed: list[str] = []
        for row in stored:
            lf = live.get(row["name"])
            if lf is not None and lf.content_hash != row["content_hash"]:
                changed.append(row["name"])
                await self._emit("skill.changed", task_id, {
                    "name": row["name"], "snapshot_hash": row["content_hash"],
                    "live_hash": lf.content_hash,
                    "note": "task continues on the durable snapshot, not the edited file"})
        return changed

    def render(self, stored: list[dict[str, Any]]) -> str:
        """Legacy shape: a bounded skills block for the prompt from stored rows. Preserved so the
        existing loop.py call site works unchanged. New callers should prefer `render_plan`."""
        if not stored:
            return ""
        # Convert stored rows into a minimal SkillPlan-like structure. This keeps the layered
        # rendering (summary vs deep sections) available even when composing from durable rows.
        from sali.skills.composer import SkillPick
        from sali.skills.discovery import SkillFile
        picks = []
        for row in stored[: self._max_skills]:
            sf = SkillFile(
                name=row["name"], title=row["name"], path=row.get("path") or "",
                content=row.get("content") or "",
                content_hash=row.get("content_hash") or "",
                summary=(row.get("content") or "")[:600])
            picks.append(SkillPick(
                skill=sf, score=float(row.get("score") or 0.0),
                kind=row.get("kind") or "supporting",
                reason=row.get("reason") or "",
                detected_version=row.get("detected_version") or ""))
        return render_summary(SkillPlan(picks=picks), per_skill_chars=self._per_skill_chars,
                              max_skills=self._max_skills)

    def render_plan(self, plan: SkillPlan, *, sections: list[str] | None = None) -> str:
        """Render a live SkillPlan (from compose_for_chat) — summary by default, deep sections
        when the caller asks for them."""
        if sections:
            return render_deep(plan, section_titles=sections,
                               per_section_chars=1200, max_primary=2)
        return render_summary(plan, per_skill_chars=self._per_skill_chars,
                              max_skills=self._max_skills)

    async def _emit(self, event_type: str, task_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, subject_type="task", subject_id=task_id,
                origin="skills", data=data)
