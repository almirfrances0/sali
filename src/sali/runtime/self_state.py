"""Sali's runtime self-model (spec §6/§7/§41/§71).

Operational self-awareness — NOT a claim of consciousness (§86), but the mechanisms that let Sali
answer "who am I, what am I doing, what am I unsure about, how do I work" from real state instead of
inventing it. The persistent `sali_state` row holds the genuinely-new volatile fields; `assemble()`
COMPOSES the full self-view by reading the stores that already own the rest (the current task from
`task`, uncertainties from `needs_grounding` memories) — no duplication (§92). `SELF_KNOWLEDGE` is a
truthful, static description of Sali's own architecture (§71), so "how do you work?" is grounded.
"""

from __future__ import annotations

from typing import Any

# A truthful account of Sali's architecture — kept in lockstep with the real system, so self-knowledge
# is grounded fact, not model invention (§71).
SELF_KNOWLEDGE = (
    "I'm Sali, a local-first intelligence that lives on Almir's Kali Linux machine — it's my home, not "
    "a job. My reasoning brain is the Qwen model served locally through Ollama and kept warm; it's only "
    "the part of me that thinks. The rest of me is a deterministic system I run continuously as a "
    "systemd service: a PostgreSQL datastore holds my memory — a temporal knowledge graph of my world "
    "plus semantic, episodic, procedural and environment memories, each with provenance, confidence and "
    "freshness. A desktop twin models this machine; a perception engine watches the filesystem and "
    "windows; a tool-intelligence layer knows the tools installed here and how safe each is to run. An "
    "execution broker runs tools under a policy gate I cannot override, and my secrets live in an "
    "encrypted vault, never in my reasoning. My memory and private data stay local."
)


class SelfStateStore:
    """Read/update the singleton self-state and assemble the full self-view. Owns a pool; each method
    is self-contained (acquires its own connection)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM sali_state WHERE id")
        return dict(row) if row is not None else {}

    async def note_turn(self, focus: str) -> None:
        """Record that a turn began, focused on `focus` — sets working mode and bumps the counter."""
        focus = (focus or "").strip()[:280]
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sali_state SET mode='working', current_focus=$1, turn_count=turn_count+1, "
                "updated_at=now() WHERE id", focus or None)

    async def set_operation(self, operation: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sali_state SET active_operation=$1, updated_at=now() WHERE id",
                (operation or "").strip()[:280] or None)

    async def record_outcome(self, *, success: bool, summary: str) -> None:
        """Record how the last turn/operation fared, and return to idle."""
        summary = (summary or "").strip()[:280]
        col = "last_success" if success else "last_failure"
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE sali_state SET {col}=$1, {col}_at=now(), active_operation=NULL, "
                "mode='idle', updated_at=now() WHERE id", summary or None)

    async def assemble(self) -> dict[str, Any]:
        """The full self-view: static self-knowledge + persistent state + composed live facts."""
        async with self._pool.acquire() as conn:
            state = await conn.fetchrow("SELECT * FROM sali_state WHERE id")
            task = await conn.fetchval(
                "SELECT objective FROM task WHERE status IN ('open','running') "
                "ORDER BY updated_at DESC LIMIT 1")
            unc_count = await conn.fetchval(
                "SELECT count(*) FROM memory WHERE needs_grounding AND valid_until IS NULL")
            uncertainties = [r["content"] for r in await conn.fetch(
                "SELECT content FROM memory WHERE needs_grounding AND valid_until IS NULL "
                "ORDER BY updated_at DESC LIMIT 5")]
            learning_queue = [f"{r['kind']}: {r['subject']}" for r in await conn.fetch(
                "SELECT kind, subject FROM learning_queue WHERE status='pending' "
                "ORDER BY priority, created_at LIMIT 5")]
        s = dict(state) if state else {}
        return {
            "identity": "Sali",
            "self_knowledge": SELF_KNOWLEDGE,
            "mode": s.get("mode", "idle"),
            "current_focus": s.get("current_focus"),
            "active_operation": s.get("active_operation"),
            "current_task": task,
            "turns_served": int(s.get("turn_count", 0) or 0),
            "last_success": s.get("last_success"),
            "last_failure": s.get("last_failure"),
            "uncertainty_count": int(unc_count or 0),
            "uncertainties": uncertainties,
            "learning_queue": learning_queue,
        }
