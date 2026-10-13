"""Relevance-aware knowledge retrieval for adaptive planning (Prompt 7 §20/§33/§34/§35).

Before/while working a task, surface ONLY the learned knowledge relevant to it — similar past lessons,
known failure modes, verified techniques — ranked by relevance + evidence + recency, with contradicted
knowledge excluded. Deterministic keyword+scope+evidence scoring (no embeddings needed for correctness,
so it's testable offline; pgvector semantic recall is a future signal, §34). Bounded output so learned
knowledge participates in the 20-25K context budget without ever crowding out the current task (§33).

Lives in the learning layer, so it reads durable ``learning_candidate`` + ``memory`` directly and never
imports the retrieval layer above it.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

_STOP = frozenset((
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are", "be", "it",
    "this", "that", "use", "using", "build", "create", "make", "app", "application", "task", "add",
))


def _keywords(*texts: str | None) -> set[str]:
    words: set[str] = set()
    for t in texts:
        for w in re.findall(r"[a-z0-9]+", (t or "").lower()):
            if len(w) >= 3 and w not in _STOP:
                words.add(w)
    return words


def _score(row: dict[str, Any], keys: set[str], scope_ref: str | None) -> float:
    lesson_words = _keywords(row.get("lesson"))
    overlap = len(lesson_words & keys)
    score = float(overlap)
    score += 0.5 * int(row.get("evidence_level") or 0)          # stronger evidence ranks higher (§35)
    score += float(row.get("confidence") or 0)
    if scope_ref and row.get("scope_ref") and row["scope_ref"] == scope_ref:
        score += 2.0                                            # same project/skill/environment (§35)
    if row.get("verification_state") == "promoted":
        score += 1.0
    return score


async def relevant_knowledge(
    pool: Any, *, objective: str = "", skills: list[str] | None = None, error: str | None = None,
    scope_ref: str | None = None, limit: int = 6, publisher: Any = None,
) -> list[dict[str, Any]]:
    """The learned knowledge most relevant to the current situation, best first, contradicted excluded.
    Emits ``knowledge.retrieved`` (count only — never chain-of-thought). Bounded by ``limit``."""
    keys = _keywords(objective, error, " ".join(skills or []))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, lesson, scope, scope_ref, source_type, evidence_level, confidence, "
            "  verification_state, times_successful, times_failed FROM learning_candidate "
            "WHERE verification_state IN ('verified','promoted','supported') "
            "ORDER BY evidence_level DESC, confidence DESC, updated_at DESC LIMIT 200")
    # keep only items with a RELEVANCE signal — keyword overlap or the same project/skill/env (§35).
    # Intrinsic evidence alone (level/confidence) never makes an unrelated lesson relevant — no noise.
    def _relevant(r: dict[str, Any]) -> bool:
        return bool(_keywords(r.get("lesson")) & keys) or (
            scope_ref is not None and r.get("scope_ref") == scope_ref)

    candidates = [dict(r) for r in rows if _relevant(dict(r))]
    candidates.sort(key=lambda r: _score(r, keys, scope_ref), reverse=True)
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in candidates:
        key = " ".join((r["lesson"] or "").lower().split())
        if key in seen:
            continue
        seen.add(key)
        chosen.append(r)
        if len(chosen) >= limit:
            break
    if publisher is not None and chosen:
        with contextlib.suppress(Exception):
            await publisher.emit(event_type="knowledge.retrieved", subject_type="learning",
                                 origin="learning", data={"count": len(chosen),
                                                          "objective": objective[:120]})
    return chosen


def render_hints(items: list[dict[str, Any]], *, per_item_chars: int = 200, max_items: int = 5) -> str:
    """A bounded block of relevant learned knowledge for the context (§33) — low priority: the current
    task always wins, so this is meant to be dropped first under pressure. Guidance, not proof."""
    if not items:
        return ""
    parts = ["LEARNED KNOWLEDGE (from past tasks — evidence-ranked; guidance, verify before relying):"]
    for r in items[:max_items]:
        lesson = (r.get("lesson") or "").strip()
        if len(lesson) > per_item_chars:
            lesson = lesson[:per_item_chars].rstrip() + " …"
        tag = "negative" if (r.get("source_type") or "") in ("failure", "negative", "recurring_failure") else \
            r.get("verification_state", "")
        parts.append(f"- [{tag}] {lesson}")
    return "\n".join(parts)
