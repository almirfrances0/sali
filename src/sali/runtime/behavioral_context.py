"""Behavioral-context assembly (§19/§30) — bounded, derived, read-only diagnostic view.

Behavioral continuity does not come from an ever-growing system prompt or from keeping years of chat
in context (§30). It is DERIVED, on demand and bounded, from durable state: accepted behavioral
tendencies (behavior_proposal, from Prompt-7 behavior evolution), user preferences, and the current
relationship context (person). This module ASSEMBLES a compact view; it owns no data and mutates
nothing.

**Brain-audit turn 7** removed the renderer companion and its standing-requests prompt injection
— accepted preferences now surface through the normal memory-retrieval bundle, gated by an actual
acceptance decision instead of the auto-elevation that turned every classifier misfire into a
permanent prompt anchor. `assemble_behavioral_context` stays here only as a read-only diagnostic
view for the /api/v1/behavioral-context HTTP endpoint; it is NOT wired into the prompt.
"""

from __future__ import annotations

import contextlib
from typing import Any


async def assemble_behavioral_context(
    pool: Any, *, person_name: str | None = None, limit: int = 6,
) -> dict[str, Any]:
    """A bounded, derived behavioral view for external inspection (§19). Accepted behavior
    proposals + relationship shell. Read-only — never routed back to the model."""
    preferences: list[str] = []
    tendencies: list[str] = []
    relationship: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        from sali.learning.behavior import BehaviorStore
        accepted = await BehaviorStore(pool).accepted(limit=limit * 3)
        for a in accepted:
            (preferences if a["scope"] == "user" else tendencies).append(a["proposed_behavior"])
    if person_name is not None:
        with contextlib.suppress(Exception):
            from sali.tasks.people import PersonStore
            relationship = await PersonStore(pool).get(person_name)
    return {"preferences": preferences[:limit], "tendencies": tendencies[:limit],
            "relationship": _relationship_summary(relationship)}


def _relationship_summary(person: dict[str, Any] | None) -> dict[str, Any] | None:
    if person is None:
        return None
    return {"name": person.get("name"), "relationship": person.get("relationship_type"),
            "preferred_channel": person.get("preferred_channel"),
            "provenance": person.get("provenance")}
