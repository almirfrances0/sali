"""Procedure acquisition (spec §17): a recurring command sequence becomes a procedural memory.

The evidence bar is the whole point: a procedure is NEVER created from a single observation — it
must have run in at least ``threshold`` distinct runs (repeated evidence). The deterministic
mining finds the candidate; the model does one small INTERPRET step — naming it — because a good
name ("Docker deploy") is a judgement, unlike detecting the repeat. Learned as an INFERENCE-
sourced memory (confidence below a direct observation, per the confidence model) so it stays
honestly weaker than something Sali saw first-hand, and grows as the evidence does.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.enums import MemoryLayer, MemorySource
from sali.learning.mining import normalize_command, sequences_by_run, signature
from sali.learning.model import LearnedProcedure
from sali.memory import writer as memory_writer
from sali.provider.base import ChatMessage, ModelProvider

_NAME_OPTS: dict[str, Any] = {"temperature": 0.3, "top_k": 40, "top_p": 0.9}
_NAME_SYSTEM = (
    "Name this recurring sequence of shell commands as a short procedure — 2 to 5 words, like a "
    "label Almir would recognise (e.g. 'Docker deploy', 'DB backup'). Reply with ONLY the name."
)


async def _name_procedure(provider: ModelProvider, steps: list[str]) -> str:
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_NAME_SYSTEM),
             ChatMessage(role="user", content="\n".join(steps))],
            options=_NAME_OPTS,
        )
        name = res.content.strip().splitlines()[0].strip(" .\"'`") if res.content.strip() else ""
    except Exception:  # noqa: BLE001 - naming is best-effort; fall back to the steps
        name = ""
    return name or " → ".join(steps)[:60]


async def learn_procedures(
    conn: Any, provider: ModelProvider, *, threshold: int = 2, limit: int = 500
) -> list[LearnedProcedure]:
    """Mine successful commands into procedures that cleared the evidence bar. Caller owns the txn."""
    rows = await conn.fetch(
        "SELECT run_id, plan->'args'->>'command' AS command FROM tool_execution "
        "WHERE tool_name='execute_command' AND success IS TRUE "
        "  AND plan->'args'->>'command' IS NOT NULL "
        "ORDER BY run_id, started_at LIMIT $1",
        limit,
    )
    learned: list[LearnedProcedure] = []
    for steps, runs in sequences_by_run([dict(r) for r in rows]).items():
        if len(runs) < threshold:
            continue  # not enough evidence yet — never learn from one observation (§17)
        sig = signature(steps)
        # Reuse the name we already gave this procedure. The model name is non-deterministic, so
        # re-naming on every pass would rewrite the content under a stable claim_key — churning the
        # memory and logging false contradictions. Same name → same content → a clean corroboration.
        existing = await conn.fetchrow(
            "SELECT structured FROM memory WHERE claim_key=$1 AND valid_until IS NULL",
            f"procedure:{sig}",
        )
        name = (existing["structured"] or {}).get("name") if existing else None
        if not name:
            name = await _name_procedure(provider, list(steps))
        await memory_writer.remember(
            conn, layer=MemoryLayer.PROCEDURAL,
            content=f"{name} — Almir's usual steps: " + " → ".join(steps),
            source=MemorySource.INFERENCE, functional=True, claim_key=f"procedure:{sig}",
            importance=0.7, obs_conf=min(0.9, 0.5 + 0.1 * len(runs)),
            # certainty hierarchy (§23): a mined-and-repeated sequence is 'learned' (past 'observed');
            # 'preferred' is reserved for one Almir has explicitly confirmed.
            structured={"steps": list(steps), "evidence": len(runs), "name": name,
                        "certainty": "learned"},
        )
        learned.append(LearnedProcedure(name=name, steps=list(steps), evidence=len(runs)))
    return learned


async def record_procedure_outcomes(conn: Any, *, days: int = 7, limit: int = 1000) -> int:
    """Close the learning loop (§43): when a run executed a KNOWN procedure's exact sequence, reinforce
    the procedure if the run was clean and penalize it if the run hit a failure — so a procedure that
    keeps working grows trusted and one that keeps failing loses confidence. Each (procedure, run)
    outcome is counted once (deduped via memory_evidence.source_ref). Returns outcomes recorded."""
    procs = {
        r["claim_key"]: r for r in await conn.fetch(
            "SELECT id, claim_key, confidence FROM memory "
            "WHERE layer='procedural'::memory_layer AND valid_until IS NULL AND claim_key LIKE 'procedure:%'")
    }
    if not procs:
        return 0
    rows = await conn.fetch(
        "SELECT run_id, plan->'args'->>'command' AS command, success FROM tool_execution "
        "WHERE tool_name='execute_command' AND plan->'args'->>'command' IS NOT NULL "
        "  AND started_at > now() - make_interval(days => $1) ORDER BY run_id, started_at LIMIT $2",
        days, limit)

    per_run: dict[UUID, dict[str, Any]] = {}
    for r in rows:
        state = per_run.setdefault(r["run_id"], {"steps": [], "failed": False})
        if r["success"] is False:
            state["failed"] = True
        norm = normalize_command(str(r["command"]))
        if norm and (not state["steps"] or state["steps"][-1] != norm):
            state["steps"].append(norm)

    recorded = 0
    for run_id, state in per_run.items():
        if len(state["steps"]) < 2:
            continue
        proc = procs.get(f"procedure:{signature(tuple(state['steps']))}")
        if proc is None:
            continue  # this run's sequence isn't a known procedure
        if await conn.fetchval(
            "SELECT 1 FROM memory_evidence WHERE memory_id=$1 AND source_ref=$2", proc["id"], run_id):
            continue  # this application was already scored
        success = not state["failed"]
        if success:
            await conn.execute(
                "UPDATE memory SET confidence=least(0.95, confidence + 0.03), updated_at=now() WHERE id=$1",
                proc["id"])
            ev_conf, note = 0.9, "procedure applied successfully"
        else:
            await conn.execute(
                "UPDATE memory SET confidence=greatest(0.2, confidence - 0.1), updated_at=now() WHERE id=$1",
                proc["id"])
            ev_conf, note = 0.2, "procedure application hit a failure"
        await conn.execute(
            "INSERT INTO memory_evidence (memory_id, source, source_ref, confidence, note) "
            "VALUES ($1,'procedure_execution'::memory_source,$2,$3,$4)",
            proc["id"], run_id, ev_conf, note)
        recorded += 1
    return recorded
