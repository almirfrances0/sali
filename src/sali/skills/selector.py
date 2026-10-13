"""Deterministic skill selection (Prompt 5 §8) — a first-pass selector that does NOT rely on the LLM.

Scores each discovered skill against the task objective (and the triggering message) by tag / name /
title overlap, and returns the top few above a threshold. Selecting only the relevant skills keeps the
context bounded (§12) instead of dumping every skill every turn.
"""

from __future__ import annotations

import re

from sali.skills.discovery import SkillFile

_TAG_HIT = 2.0     # a skill tag appears in the request — the strongest signal
_NAME_HIT = 1.5    # the skill's own slug appears in the request
_TITLE_WORD = 0.5  # a word of the skill's title appears in the request


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1}


def select_skills(
    skills: list[SkillFile], *, objective: str, message: str = "",
    limit: int = 3, min_score: float = 1.0,
) -> list[tuple[SkillFile, float]]:
    """Rank skills for a task; return up to `limit` (skill, score) pairs scoring at least `min_score`,
    highest first. Deterministic and pure — same inputs always give the same selection."""
    text = f"{objective} {message}".lower()
    words = _tokens(text)
    scored: list[tuple[SkillFile, float]] = []
    for s in skills:
        score = 0.0
        for tag in s.tags:
            t = tag.lower()
            if t in words or (len(t) > 3 and t in text):
                score += _TAG_HIT
        if s.name in words or s.name in text:
            score += _NAME_HIT
        for w in _tokens(s.title):
            if w in words:
                score += _TITLE_WORD
        if score > 0:
            scored.append((s, round(score, 2)))
    scored.sort(key=lambda x: (-x[1], x[0].name))  # score desc, then name for stable ties
    return [(s, sc) for s, sc in scored if sc >= min_score][:limit]
