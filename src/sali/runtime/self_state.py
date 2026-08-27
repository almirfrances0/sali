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

# The INVARIANT part of Sali's architecture (no machine/model mention — those are read from the graph
# so a hardware/model swap updates the self-model automatically, no drift — §71/§10).
_REST_NARRATIVE = (
    " The rest of me is a deterministic system I run continuously as a systemd service: a PostgreSQL "
    "datastore holds my memory — a temporal knowledge graph of my world plus semantic, episodic, "
    "procedural and environment memories, each with provenance, confidence and freshness. A desktop twin "
    "models this machine; a perception engine watches the filesystem and windows; a tool-intelligence "
    "layer knows the tools installed here and how safe each is to run. An execution broker runs tools "
    "under a policy gate I cannot override, and my secrets live in an encrypted vault, never in my "
    "reasoning. My memory and private data stay local."
)
_GENERIC_BRAIN = (" My reasoning brain is a local model served through Ollama and kept warm; it's only "
                  "the part of me that thinks.")
# Stock description (fallback when the graph has no agent facts yet); assemble() prefers the grounded one.
SELF_KNOWLEDGE = ("I'm Sali, a local-first intelligence living on Almir's machine — it's my home, not a "
                  "job." + _GENERIC_BRAIN + _REST_NARRATIVE)


def _compose_self_knowledge(env: dict[str, Any]) -> str:
    """Build the self-description from live graph facts (machine + model) + the invariant narrative, so
    it never disagrees with reality the way a hardcoded string would."""
    machine = env.get("machine") or "Almir's machine"
    os_name = env.get("os")
    model = env.get("model")
    # Only add the OS in parens when it says something the machine name doesn't already.
    where = f"living on {machine}" + (f" ({os_name})" if os_name and os_name != machine else "")
    brain = (f" My reasoning brain is {model}, served locally through Ollama and kept warm; it's only "
             "the part of me that thinks." if model else _GENERIC_BRAIN)
    return f"I'm Sali, a local-first intelligence {where} — it's my home, not a job.{brain}{_REST_NARRATIVE}"


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
            await self._emit_presence(conn, "working")

    @staticmethod
    async def _emit_presence(conn: Any, mode: str) -> None:
        """Push a presence change onto the durable bus so a live UI reflects idle↔working the instant
        it happens (§27). Payload is minimal (mode only) — the UI re-reads the redacted self-view;
        the raw focus text is never persisted here."""
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, payload) VALUES ('self.presence','self',$1)",
            {"mode": mode})

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
            await self._emit_presence(conn, "idle")

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
            env = await self._environment(conn)  # machine/model/workspace/source from the graph (§1/§10)
        s = dict(state) if state else {}
        return {
            "identity": "Sali",
            "self_knowledge": _compose_self_knowledge(env),  # grounded in graph facts, no drift
            "environment": env,
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

    async def _environment(self, conn: Any) -> dict[str, Any]:
        """Sali's own environment, read from the agent subgraph the linker built (§1): the machine it
        runs on (+ os/kernel/arch), the model it thinks with, and its workspace + source locations."""
        rows = await conn.fetch(
            "SELECT e.rel_type, n.name, n.props FROM graph_edge e "
            "  JOIN graph_node a ON a.id=e.src_id JOIN graph_node n ON n.id=e.dst_id "
            "WHERE a.canonical_key='agent:sali' AND a.valid_until IS NULL "
            "  AND e.valid_until IS NULL AND n.valid_until IS NULL")
        env: dict[str, Any] = {}
        for r in rows:
            props = r["props"] or {}
            if r["rel_type"] == "runs_on":
                env.update(machine=r["name"], os=props.get("os"), kernel=props.get("kernel"),
                           arch=props.get("arch"))
            elif r["rel_type"] == "thinks_with":
                env["model"] = r["name"]
            elif r["rel_type"] == "works_in":
                env["workspace"] = props.get("path")
            elif r["rel_type"] == "source_at":
                env["source"] = props.get("path")
        return env
