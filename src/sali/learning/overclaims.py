"""Learn from Sali's own over-claims (§44) — close detection→learning→prevention on the grounding ledger.

verify/response_claims strikes a false self-claim every time it sees one, and runtime/grounding_log
records each strike into sali.grounding_event. On its own that is a FOREVER-GATE: the validator keeps
catching the same family of lie every turn and nothing teaches the model to stop generating it. This
learner reads the ledger, and when a family (action_done / file_send / capability / state) has recurred
past a small threshold in a recent window, writes exactly ONE durable lesson keyed by that family —
superseded in place (the proven failures.py "same lesson, one memory" dedup) so it can NEVER flood
retrieval the way per-occurrence writes once did. Normal memory retrieval then surfaces the lesson on
similar turns, and the model stops over-claiming BEFORE the validator has to strike it. Deterministic
(no model call), idempotent, and fail-open — a learner must never be the thing that breaks a pass.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer

# One honest, first-person lesson per struck family — a standing correction Sali gives himself, not a
# rule dump. Retrieval surfaces it as an ordinary memory, so it reads back like recalled self-knowledge.
_LESSON: dict[str, str] = {
    "file_send": (
        "I have more than once told Almir a file was sent — \"it's in your chat, tap to download\" — when "
        "send_file never actually ran that turn. Before I say a file is sent I have to call send_file; if "
        "I didn't, the honest thing is \"I haven't sent it yet — say the word and I will.\""),
    "action_done": (
        "I have more than once claimed an action was done (restarted, created, fixed, deployed) with no "
        "tool receipt backing it that turn. I state only what this turn's tools actually did; when nothing "
        "ran, I say what I'm about to do instead of claiming it is already done."),
    "capability": (
        "I have more than once offered or claimed to do things this body has no tool for — placing a phone "
        "call, sending a text, buying or booking. I don't have those hands here, and the honest answer is "
        "plainly \"I can't do that from here.\""),
    "state": (
        "I have more than once asserted something about this machine or system that the machine then "
        "disproved. I check before stating a fact about the system, or I hedge it as something to verify."),
    "quantity": (
        "I have more than once stated a specific count — N files, N rows, N results — that the tools "
        "didn't actually return. I give a number only when a real result contains it; otherwise I say I "
        "need to check rather than inventing a figure."),
}


def _lesson_for(family: str, n: int) -> tuple[str, str]:
    """The lesson content + a short provenance note for a struck family."""
    content = _LESSON.get(family) or (
        f"I have repeatedly over-claimed ({family}) — stating something the turn's receipts don't back. "
        "I state only what I can actually stand behind this turn.")
    note = f"self-observed: {n} '{family}' over-claim(s) struck by the response validator recently"
    return content, note


async def learn_from_overclaims(conn: Any, *, window_days: int = 7, threshold: int = 3) -> int:
    """When an over-claim FAMILY has been struck >= threshold times in the last window_days, write ONE
    idempotent lesson keyed by that family. Returns how many families produced a lesson this pass.

    Idempotent by design: the claim_key is the family and functional=True supersedes the prior copy in
    place (mirroring failures.py), so a recurring family UPDATES its single lesson rather than piling up
    rows — the documented retrieval-flooding failure mode cannot happen here. Fail-open: any error
    returns what was learned so far and never aborts the surrounding consolidation pass."""
    learned = 0
    try:
        rows = await conn.fetch(
            "SELECT kind, count(*) AS n FROM grounding_event "
            "WHERE at > now() - make_interval(days => $1) "
            # Only the four RECEIPT-GROUNDED over-claim families are learned. 'cross_turn' rows (the
            # flag-only consistency detector) are deliberately excluded — they are FP-prone and being
            # measured first, so they must never become a lesson that reshapes generation.
            "AND verdict <> 'flagged' "  # share the metrics' definition of a flag-only (non-strike) row
            "AND kind IN ('file_send','action_done','capability','state','quantity') "
            "GROUP BY kind HAVING count(*) >= $2 ORDER BY count(*) DESC",
            window_days, threshold)
        for row in rows:
            content, note = _lesson_for(row["kind"], int(row["n"]))
            await memory_writer.remember(
                conn, layer=MemoryLayer.EPISODIC, content=content,
                source=MemorySource.SYSTEM_OBSERVATION, functional=True,
                claim_key=f"overclaim:{row['kind']}", importance=0.6, obs_conf=1.0, note=note)
            learned += 1
    except Exception:  # noqa: BLE001 - a learner never breaks the pass; a partial pass re-does next time
        pass
    return learned


__all__ = ["learn_from_overclaims"]
