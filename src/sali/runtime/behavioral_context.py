"""Behavioral-context assembly (§19/§30) — a bounded "how I should interact right now", derived.

Behavioral continuity does not come from an ever-growing system prompt or from keeping years of chat in
context (§30). It is DERIVED, on demand and bounded, from durable state: accepted behavioral tendencies
(behavior_proposal, from Prompt-7 behavior evolution), user preferences, and the current relationship
context (person). Selection follows the existing source-priority order — a current explicit instruction
beats a durable inference (§29) — because the underlying stores already carry provenance/confidence.
This module only ASSEMBLES + renders a compact block; it owns no data and mutates nothing.
"""

from __future__ import annotations

import contextlib
from typing import Any


async def assemble_behavioral_context(
    pool: Any, *, person_name: str | None = None, limit: int = 6,
) -> dict[str, Any]:
    """The relevant, bounded behavioral context for interacting now (§19). Preferences + tendencies from
    accepted behavior proposals (evidence-backed, not hardcoded traits), plus the relationship shell.
    Never the whole behavioral history — only what matters for this interaction."""
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


def render_behavioral_context(ctx: dict[str, Any], *, max_items: int = 4) -> str:
    """A compact block for the operational context (§19) — bounded; grounded in evidence, not personality
    fabrication. Empty string when there's nothing durable to say (never invents tendencies)."""
    prefs, tends = ctx.get("preferences") or [], ctx.get("tendencies") or []
    rel = ctx.get("relationship")
    if not prefs and not tends and not rel:
        return ""
    parts = ["CURRENT INTERACTION CONTEXT (evidence-backed; current explicit instructions still win):"]
    if rel and rel.get("relationship"):
        parts.append(f"- Relationship: {rel['name']} ({rel['relationship']})"
                     + (f", prefers {rel['preferred_channel']}" if rel.get("preferred_channel") else ""))
    for p in prefs[:max_items]:
        parts.append(f"- Preference: {p}")
    for t in tends[:max_items]:
        parts.append(f"- Learned tendency: {t}")
    return "\n".join(parts)
