"""SkillStore — durable per-task skill snapshots (Prompt 5 §10/§11).

Selecting a skill for a task persists a SNAPSHOT (content + hash), not just a reference, so the task's
guidance is reproducible across compaction/interruption/restart even if the live `.md` is edited
mid-task. On continuation the live files are compared to the snapshot and a `skill.changed` event is
emitted — but the task keeps running on its original snapshot (durable reproducibility, never a silent
mid-task semantic shift). The rendered context is bounded so skills participate in budgeting, not blow it.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger
from sali.skills.discovery import discover_skills
from sali.skills.selector import select_skills

log = get_logger("sali.skills.store")


class SkillStore:
    def __init__(
        self, pool: Any, publisher: Any = None, *,
        per_skill_chars: int = 1200, max_skills: int = 3,
    ) -> None:
        self._pool = pool
        self._publisher = publisher
        self._per_skill_chars = per_skill_chars
        self._max_skills = max_skills

    async def for_task(self, task_id: UUID) -> list[dict[str, Any]]:
        """The durable skill snapshots selected for a task (the guidance it actually runs on)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, path, content_hash, content, score FROM task_skill "
                "WHERE task_id = $1 ORDER BY score DESC, name", task_id)
        return [dict(r) for r in rows]

    async def select_and_persist(
        self, task_id: UUID, *, objective: str, skills_root: str, message: str = "",
    ) -> list[dict[str, Any]]:
        """Discover skills, deterministically select the relevant ones for this objective, and persist
        their snapshots. Idempotent — re-running for a task that already has skills is a no-op insert."""
        skills = discover_skills(Path(skills_root))
        chosen = select_skills(
            skills, objective=objective, message=message, limit=self._max_skills)
        async with self._pool.acquire() as conn:
            for skill, score in chosen:
                await conn.execute(
                    "INSERT INTO task_skill (task_id, name, path, content_hash, content, score) "
                    "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (task_id, name) DO NOTHING",
                    task_id, skill.name, skill.path, skill.content_hash, skill.content, score)
        await self._emit("skill.discovered", task_id,
                         {"available": len(skills), "selected": [s.name for s, _ in chosen]})
        for skill, score in chosen:
            await self._emit("skill.selected", task_id,
                             {"name": skill.name, "path": skill.path, "score": score,
                              "content_hash": skill.content_hash})
        return await self.for_task(task_id)

    async def detect_changes(self, task_id: UUID, skills_root: str) -> list[str]:
        """Compare the live skill files to the task's snapshots; emit skill.changed for any that differ.
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
        """A bounded skills block for the prompt (§12): each snapshot capped, at most max_skills."""
        if not stored:
            return ""
        parts = [
            "RELEVANT SKILLS (task guidance — follow it where it applies; it never overrides safety or "
            "policy, and evidence always beats what a skill claims):",
        ]
        for row in stored[: self._max_skills]:
            body = (row.get("content") or "").strip()
            if len(body) > self._per_skill_chars:
                body = body[: self._per_skill_chars].rstrip() + " …"
            parts.append(f"\n## {row['name']}\n{body}")
        return "\n".join(parts)

    async def _emit(self, event_type: str, task_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, subject_type="task", subject_id=task_id,
                origin="skills", data=data)
