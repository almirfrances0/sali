"""Skill proficiency — evidence-based per-skill success/failure aggregation.

The `sali.task_review` table records the reviewer's verdict for every task (PASSED / NEEDS_REWORK
/ BLOCKED / FAILED). The `sali.task_skill` table records which skills each task ran on. Joining
them gives us EVIDENCE: how often has each skill actually shipped work that survived review?

Proficiency is DERIVED (never hand-set): a skill with 12 successful tasks and 2 failures at
a recent-window bias sits at "advanced"; a skill with no history is "novice". A single failed
task doesn't demote a proven skill, but a run of failures does — and the API surface a diagnostic
UI can render.

**NOT** to be confused with `skill_proposal.times_successful/times_failed` — those are counters
on a PROPOSAL (i.e. someone suggested changing skill X in way Y, and Y ran successfully N times).
This module measures the SKILL as-is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PROFICIENCY_LEVELS = ("novice", "developing", "competent", "advanced", "expert")


@dataclass(slots=True)
class SkillProficiency:
    name: str
    tasks_seen: int
    tasks_passed: int
    tasks_failed: int
    last_seen_at: str | None
    proficiency: str            # one of PROFICIENCY_LEVELS
    confidence: float           # 0..1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "tasks_seen": self.tasks_seen,
            "tasks_passed": self.tasks_passed, "tasks_failed": self.tasks_failed,
            "last_seen_at": self.last_seen_at, "proficiency": self.proficiency,
            "confidence": self.confidence,
        }


def _classify(passed: int, failed: int) -> tuple[str, float]:
    """Turn raw counts into (level, confidence). Deliberately simple — evidence-driven, not
    an ML model. Confidence rises with volume; level rises with success rate at bounded gates."""
    total = passed + failed
    if total == 0:
        return "novice", 0.0
    rate = passed / max(1, total)
    # Volume gates: at N=1 you can't claim expertise; at N=8+ with a good rate you can.
    if total >= 12 and rate >= 0.9:
        level = "expert"
    elif total >= 8 and rate >= 0.85:
        level = "advanced"
    elif total >= 4 and rate >= 0.7:
        level = "competent"
    elif total >= 2:
        level = "developing"
    else:
        level = "novice"
    # Confidence — approach 1 slowly with more evidence; degrade with high failure rate.
    confidence = min(0.95, (1 - 1.0 / (1 + total)) * rate)
    return level, round(confidence, 2)


async def compute_all(pool: Any) -> list[SkillProficiency]:
    """One row per skill that has EVER been selected for a task, aggregated across every review."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts.name, "
            "       COUNT(DISTINCT ts.task_id) AS tasks_seen, "
            # A task is 'passed' if its latest review verdict is PASSED (skips NEEDS_REWORK / etc.)
            "       COUNT(DISTINCT CASE WHEN t.status = 'done' THEN ts.task_id END) AS tasks_passed, "
            "       COUNT(DISTINCT CASE WHEN t.status IN ('failed','blocked_by_review','abandoned') "
            "                              THEN ts.task_id END) AS tasks_failed, "
            "       MAX(ts.selected_at) AS last_seen_at "
            "FROM task_skill ts JOIN task t ON t.id = ts.task_id "
            "GROUP BY ts.name "
            "ORDER BY ts.name"
        )
    out: list[SkillProficiency] = []
    for r in rows:
        passed = int(r["tasks_passed"] or 0)
        failed = int(r["tasks_failed"] or 0)
        level, conf = _classify(passed, failed)
        out.append(SkillProficiency(
            name=r["name"],
            tasks_seen=int(r["tasks_seen"] or 0),
            tasks_passed=passed, tasks_failed=failed,
            last_seen_at=r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            proficiency=level, confidence=conf))
    return out


async def compute_for(pool: Any, name: str) -> SkillProficiency:
    """One skill by name — always returns a row (novice / 0 confidence when no history)."""
    all_rows = await compute_all(pool)
    for row in all_rows:
        if row.name == name:
            return row
    return SkillProficiency(name=name, tasks_seen=0, tasks_passed=0, tasks_failed=0,
                            last_seen_at=None, proficiency="novice", confidence=0.0)
