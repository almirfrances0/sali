"""Self-reflection (spec §57).

After a substantial task, Sali steps back and asks itself the one thing worth keeping: what's the
reusable lesson for next time? That single INTERPRET step is a legitimate use of the model — unlike
mining a repeat, judging a lesson is a judgement. It is deliberately throttled and gated: only runs
that actually did meaningful work (≥ a few tool calls) are reflected on, at most one per consolidation
pass, and each run exactly once — never expensive reflection after "2+2" (§57). The lesson lands as a
SEMANTIC memory with certainty 'learned', retrievable when a similar task comes round.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.toolvocab import binary_of
from sali.memory import writer as memory_writer
from sali.provider.base import ChatMessage, ModelProvider

_REFLECT_OPTS: dict[str, Any] = {"temperature": 0.3, "top_k": 40, "top_p": 0.9}
_REFLECT_SYSTEM = (
    "You are Sali, reflecting on a task you just finished. In ONE short sentence, state the single "
    "reusable lesson worth remembering for next time — something concrete and general, not a play-by-"
    "play. If there's nothing genuinely worth keeping, reply with exactly 'none'."
)


async def reflect_on_recent(conn: Any, provider: ModelProvider, *, min_tools: int = 3) -> int:
    """Reflect on the most recent substantial, not-yet-reflected run. Returns 1 if a lesson was kept,
    else 0. Caller owns the transaction context; the model call runs without holding one open."""
    run = await conn.fetchrow(
        "SELECT r.run_id, r.user_input FROM agent_runs r "
        "WHERE r.status='completed' "
        "  AND (SELECT count(*) FROM tool_execution t WHERE t.run_id=r.run_id) >= $1 "
        "  AND NOT EXISTS (SELECT 1 FROM event e WHERE e.event_type='learning.reflected' "
        "                  AND e.subject_id=r.run_id) "
        "ORDER BY r.updated_at DESC LIMIT 1",
        min_tools)
    if run is None:
        return 0

    rows = await conn.fetch(
        "SELECT tool_name, plan->'args'->>'command' AS command, success, error "
        "FROM tool_execution WHERE run_id=$1 ORDER BY started_at LIMIT 40", run["run_id"])
    # The §36 experience facets — assembled from the run itself (no model needed for the facts).
    tools = sorted({binary_of(str(r["command"])) or r["tool_name"] for r in rows})
    errors = [str(r["error"]) for r in rows if r["success"] is False and r["error"]][:5]
    outcome = "success" if not errors else "partial — some steps failed"
    ran = ", ".join(f"{t['tool_name']}{' (failed)' if t['success'] is False else ''}" for t in rows)

    lesson = ""
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_REFLECT_SYSTEM),
             ChatMessage(role="user", content=f"Goal: {run['user_input']}\nWhat you did: {ran}")],
            options=_REFLECT_OPTS)
        raw = (res.content or "").strip()
        if raw:
            # Take the first substantive line, stripping common preamble patterns
            for line in raw.splitlines():
                clean = line.strip().strip(" .'\"")
                if clean and clean.lower() not in ("none", "here's the lesson:", "lesson:"):
                    lesson = clean
                    break
    except Exception:  # noqa: BLE001 - reflection is best-effort; a model hiccup just skips it
        lesson = ""
    if lesson.lower().strip(" .'\"") == "none":
        lesson = ""

    # Mark this run reflected regardless of the outcome, so it's never re-reflected forever.
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id) "
        "VALUES ('learning.reflected','agent_run',$1)", run["run_id"])

    # A clean run with nothing notable to say keeps nothing (§57). Otherwise write ONE structured
    # EXPERIENCE (§36) — task/tools/errors/outcome/lesson in a single episodic record, replacing the
    # old bare "Lesson learned" semantic (removes the near-duplicate the review flagged).
    if not lesson and not errors:
        return 0
    headline = lesson or f"{outcome}: {run['user_input'][:100]}"
    await memory_writer.remember(
        conn, layer=MemoryLayer.EPISODIC, content=f"Experience — {run['user_input']}: {headline}",
        source=MemorySource.INFERENCE, importance=0.6,
        structured={"kind": "experience", "task": run["user_input"][:200], "tools": tools,
                    "errors": errors, "outcome": outcome, "lesson": lesson, "certainty": "learned"})
    return 1
