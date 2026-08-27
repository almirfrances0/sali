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

    tools = await conn.fetch(
        "SELECT tool_name, success FROM tool_execution WHERE run_id=$1 ORDER BY started_at LIMIT 40",
        run["run_id"])
    ran = ", ".join(f"{t['tool_name']}{'' if t['success'] else ' (failed)'}" for t in tools)
    prompt = f"Goal: {run['user_input']}\nWhat you did: {ran}"

    lesson = ""
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_REFLECT_SYSTEM),
             ChatMessage(role="user", content=prompt)], options=_REFLECT_OPTS)
        lesson = (res.content or "").strip().splitlines()[0].strip() if res.content.strip() else ""
    except Exception:  # noqa: BLE001 - reflection is best-effort; a model hiccup just skips it
        lesson = ""

    # Mark this run reflected regardless of the outcome, so it's never re-reflected forever.
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id) "
        "VALUES ('learning.reflected','agent_run',$1)", run["run_id"])

    if not lesson or lesson.lower().strip(" .'\"") == "none":
        return 0
    await memory_writer.remember(
        conn, layer=MemoryLayer.SEMANTIC, content=f"Lesson learned: {lesson}",
        source=MemorySource.INFERENCE, importance=0.6,
        structured={"kind": "reflection", "goal": run["user_input"][:200], "certainty": "learned"})
    return 1
