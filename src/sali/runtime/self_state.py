"""Sali's runtime self-model (spec §6/§7/§41/§71).

Operational self-awareness — NOT a claim of consciousness (§86), but the mechanisms that let Sali
answer "who am I, what am I doing, what am I unsure about, how do I work" from real state instead of
inventing it. The persistent `sali_state` row holds the genuinely-new volatile fields; `assemble()`
COMPOSES the full self-view by reading the stores that already own the rest (the current task from
`task`, uncertainties from `needs_grounding` memories) — no duplication (§92). `SELF_KNOWLEDGE` is a
truthful, static description of Sali's own architecture (§71), so "how do you work?" is grounded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _is_fresh(at: Any, *, seconds: int = 3600) -> bool:
    """Whether a state timestamp is recent. Guards current_focus: an interrupted turn (CancelledError)
    never calls record_outcome, so mode can stay 'working' with a stale focus — freshness catches that."""
    if at is None:
        return False
    try:
        return (datetime.now(timezone.utc) - at).total_seconds() < seconds
    except Exception:  # noqa: BLE001 - can't compute age -> don't suppress a genuinely-working focus
        return True

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
def _compose_self_knowledge(env: dict[str, Any]) -> str:
    """Build the self-description from live graph facts (machine + model) + the invariant narrative, so
    it never disagrees with reality the way a hardcoded string would."""
    machine = env.get("machine") or "this machine"
    os_name = env.get("os")
    model = env.get("model")
    # Only add the OS in parens when it says something the machine name doesn't already.
    where = f"living on {machine}" + (f" ({os_name})" if os_name and os_name != machine else "")
    brain = (f" My reasoning brain is {model}, served locally through Ollama and kept warm; it's only "
             "the part of me that thinks." if model else _GENERIC_BRAIN)
    return f"I'm Sali, a local-first intelligence {where} — it's my home, not a job.{brain}{_REST_NARRATIVE}"


# Stock description (fallback when the graph has no agent facts yet); assemble() prefers the grounded
# one. DERIVED from the composer rather than written out beside it: the two were duplicate prose that
# had to stay byte-identical, and editing one of them was all it took to make Sali's fallback
# self-description disagree with his grounded one.
SELF_KNOWLEDGE = _compose_self_knowledge({})


class SelfStateStore:
    """Read/update the singleton self-state and assemble the full self-view. Owns a pool; each method
    is self-contained (acquires its own connection).

    EVERY WRITE UPSERTS, and that is not defensive style — it is a repair. The singleton is created by
    migration 0014, but `sali_state` carries foreign keys to `task`, so a `TRUNCATE task CASCADE`
    (exactly what a "delete everything from testing" sweep runs) deletes the row. The writes were plain
    `UPDATE ... WHERE id`, which then matched nothing and reported success, so Sali silently stopped
    recording what he was doing, how the last turn went, and how many turns he had served — while the
    presence events, being separate INSERTs, kept firing and made it look alive. Measured on the live
    database: 0 rows in `sali_state` against 161 presence events. An upsert cannot fail that way."""

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
                "INSERT INTO sali_state (id, mode, current_focus, turn_count, updated_at) "
                "VALUES (true, 'working', $1, 1, now()) "
                "ON CONFLICT (id) DO UPDATE SET mode='working', current_focus=EXCLUDED.current_focus, "
                "  turn_count = sali_state.turn_count + 1, updated_at=now()", focus or None)
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
                "INSERT INTO sali_state (id, active_operation, updated_at) VALUES (true, $1, now()) "
                "ON CONFLICT (id) DO UPDATE SET active_operation=EXCLUDED.active_operation, "
                "  updated_at=now()", (operation or "").strip()[:280] or None)

    async def record_outcome(self, *, success: bool, summary: str) -> None:
        """Record how the last turn/operation fared, and return to idle."""
        summary = (summary or "").strip()[:280]
        col = "last_success" if success else "last_failure"
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO sali_state (id, mode, {col}, {col}_at, updated_at) "
                f"VALUES (true, 'idle', $1, now(), now()) "
                f"ON CONFLICT (id) DO UPDATE SET {col}=EXCLUDED.{col}, {col}_at=now(), "
                "  active_operation=NULL, mode='idle', updated_at=now()", summary or None)
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
        _working_fresh = s.get("mode") == "working" and _is_fresh(s.get("updated_at"))
        return {
            "identity": env.get("name") or "Sali",   # from state (the agent node), no longer a literal
            "presentation": env.get("presentation"),  # §2/§3 — male / he-him, owner, preferred address,
            "pronouns": env.get("pronouns"),          # all from the graph so a claim about them is grounded
            "role": env.get("role"),
            "owner": env.get("owner"),
            "preferred_address": env.get("preferred_address"),
            "self_knowledge": _compose_self_knowledge(env),  # grounded in graph facts, no drift
            "environment": env,
            "mode": s.get("mode", "idle"),
            # current_focus is written at turn START and never cleared at turn END, so surfacing it
            # present-tense on an IDLE Sali reports a stale focus as a live fact (measured: idle, yet
            # focus="great, send me that file" from 65 min ago). An old state presented as current is its
            # own hallucination — the same discipline _self_note already applies to last_failure. Gate to
            # the working state; record_outcome returns to idle, so an idle Sali honestly reports no live
            # focus. Both readers (the self_state tool and the /current-state snapshot) go through here.
            # working AND fresh — an interrupted turn can leave mode='working' with an hour-old focus.
            "current_focus": s.get("current_focus") if _working_fresh else None,
            "active_operation": s.get("active_operation") if _working_fresh else None,
            "focus_at": s.get("updated_at"),  # so a snapshot can age/stale-flag the focus (§ temporal)
            "current_task": task,
            "turns_served": int(s.get("turn_count", 0) or 0),
            "last_success": s.get("last_success"),
            "last_failure": s.get("last_failure"),
            # The timestamps come with them so a reader can tell "this just went wrong" from "this went
            # wrong last week". Without them the only honest thing to do with a failure is ignore it.
            "last_success_at": s.get("last_success_at"),
            "last_failure_at": s.get("last_failure_at"),
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
                # this host IS Sali's home/body (§1) — a structural fact from the machine node, not prose
                env["is_home"] = props.get("is_self") == "true" or props.get("role") == "home"
                if props.get("machine_id"):
                    env["machine_id"] = props.get("machine_id")
            elif r["rel_type"] == "thinks_with":
                env["model"] = r["name"]
            elif r["rel_type"] == "works_in":
                env["workspace"] = props.get("path")
            elif r["rel_type"] == "source_at":
                env["source"] = props.get("path")
        # Sali's OWN identity facts, from the agent:sali NODE props (not its edges) — structured state,
        # not prose, so "who are you / are you male / who is your owner / how should you address me" all
        # answer from the graph (§2/§3). Absent props just leave the fields unset (the caller falls back).
        try:
            arow = await conn.fetchrow(
                "SELECT name, props FROM graph_node "
                "WHERE canonical_key='agent:sali' AND valid_until IS NULL")
            if arow is not None:
                env["name"] = arow["name"]
                _p = arow["props"] or {}
                for _k in ("presentation", "pronouns", "role", "owner", "preferred_address"):
                    if _p.get(_k):
                        env[_k] = _p.get(_k)
        except Exception:  # noqa: BLE001 - identity is best-effort; never break the self-view
            pass
        return env
