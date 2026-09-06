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
from sali.provider.base import ChatMessage
from sali.provider.presets import CREATIVE
from sali.provider.base import ModelProvider

# Brain-audit Turn 6: _REFLECT_OPTS deleted; preset=CREATIVE used at call site so
# reflection is open-ended (not seed=42-locked to "same thought again" every time).
_REFLECT_SYSTEM = (
    "You are Sali, reflecting on a task you just finished. In ONE short sentence, state the single "
    "reusable lesson worth remembering for next time — something concrete and general, not a play-by-"
    "play. If there's nothing genuinely worth keeping, reply with exactly 'none'."
)


async def reflect_on_recent(conn: Any, provider: ModelProvider, *, min_tools: int = 3) -> int:
    """Reflect on the most recent substantial, not-yet-reflected run. Returns 1 if a lesson was kept,
    else 0. Caller owns the transaction context; the model call runs without holding one open."""
    # SKIP INTERNAL BACKGROUND TURNS. Sali's own housekeeping runs (grounding checks, task
    # verification, self-directed inputs) all start with a marker in `user_input`. Reflecting on them
    # produced 197 episodic memories with content like "Experience - TASK: Verify stored procedure
    # for creating a Python script..." which then matched every subsequent task query and landed in
    # context on average 11.94 times per row - the "conversation history becomes memory" failure
    # mode Prompt A audit warns about. Almir's own words: "task history != memory". Skipping these
    # keeps reflection on real user-facing work.
    run = await conn.fetchrow(
        "SELECT r.run_id, r.user_input FROM agent_runs r "
        "WHERE r.status='completed' "
        "  AND r.user_input NOT LIKE 'TASK:%' "
        "  AND r.user_input NOT LIKE '[my own background check]%' "
        "  AND r.user_input NOT LIKE 'You wrote this down but never checked it%' "
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
            preset=CREATIVE)
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
    # A MEMORY IS A LESSON. Prompt A audit: "conversation may produce memories, but neither should
    # automatically BE memory." The old writer wrote the task's whole user_input into the content,
    # which turned every completed run into a keyword-rich noise memory that dominated retrieval on
    # any related query. Now: no lesson → no memory (the tool_execution rows and task record already
    # preserve WHAT HAPPENED; those live in their own tables for audit and don't need to become
    # semantic content the retriever surfaces). If there IS a lesson, it lands as the memory itself;
    # the task/tools/errors go into structured for context, never into keyword-searchable content.
    if not lesson:
        return 0
    await memory_writer.remember(
        conn, layer=MemoryLayer.EPISODIC, content=lesson,
        source=MemorySource.INFERENCE, importance=0.6,
        structured={"kind": "experience", "task": run["user_input"][:200], "tools": tools,
                    "errors": errors, "outcome": outcome, "lesson": lesson, "certainty": "learned"})
    return 1
