"""memory_audit — inspect what Sali actually KNOWS about something (spec §8).

For a subject, this reports over the real memory + graph: what is known, its epistemic kind (observed/
inferred/believed…), where it came from and the evidence chain behind it, when it was learned and last
verified, whether it's grounded or still a belief, any superseded versions or contradiction records, the
entities it connects to, and the procedures that depend on it. It's how Sali checks itself before
asserting something important — grounding the "I know this because …" the directive requires (§12).
Read-only, over the actual stores (no reconstruction).
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, MemoryLayer, MemorySource, RiskLevel
from sali.core.knowledge import classify_knowledge, epistemic_status
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext


async def _audit_one(conn: Any, m: dict[str, Any]) -> dict[str, Any]:
    ktype = classify_knowledge(MemorySource(m["source"]), MemoryLayer(m["layer"]),
                               needs_grounding=m["needs_grounding"], confidence=m["confidence"])
    evidence = [
        {"source": r["source"], "note": r["note"], "confidence": round(r["confidence"], 2),
         "observed_at": r["observed_at"].isoformat()}
        for r in await conn.fetch(
            "SELECT source, note, confidence, observed_at FROM memory_evidence "
            "WHERE memory_id=$1 ORDER BY observed_at", m["id"])]
    superseded = []
    if m["claim_key"]:
        superseded = [
            {"content": r["content"], "source": r["source"], "retired_at": r["valid_until"].isoformat()}
            for r in await conn.fetch(
                "SELECT content, source, valid_until FROM memory WHERE claim_key=$1 "
                "AND valid_until IS NOT NULL ORDER BY valid_until DESC LIMIT 3", m["claim_key"])]
    contradictions = await conn.fetchval(
        "SELECT count(*) FROM contradiction WHERE subject_type='memory' AND (old_id=$1 OR new_id=$1)",
        m["id"])
    return {
        "content": m["content"],
        "knowledge_type": ktype.value,
        "epistemic_status": epistemic_status(ktype, m["confidence"]),
        "source": m["source"], "confidence": round(m["confidence"], 2),
        "learned_at": m["valid_from"].isoformat() if m["valid_from"] else None,
        "last_verified": m["last_verified"].isoformat() if m["last_verified"] else None,
        "verification_status": "unverified" if m["needs_grounding"] else "grounded",
        "scope": m["scope"],
        "evidence": evidence, "evidence_count": len(evidence),
        "superseded_versions": superseded,
        "contradiction_records": int(contradictions or 0),
        "detail": m["structured"] or None,
    }


class MemoryAudit(Tool):
    name = "memory_audit"
    description = (
        "Audit what you actually KNOW about a subject before asserting it: what you know, how you know it "
        "(observed / inferred / believed), where it came from and the supporting evidence, when you "
        "learned and last verified it, whether it's grounded or unverified, any superseded or "
        "contradictory versions, the entities it connects to, and the procedures that depend on it. Use "
        "it to check yourself — it distinguishes 'I know this' from 'I could generate an answer'."
    )
    parameters = {
        "type": "object",
        "properties": {"subject": {"type": "string", "description": "The fact/entity to audit."}},
        "required": ["subject"],
    }
    risk_level = RiskLevel.R0
    capabilities = frozenset({Capability.READ})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.pool is None:
            return ToolResult(ok=False, display="no datastore", error="memory audit isn't available")
        subject = str(args.get("subject", "")).strip()
        if not subject:
            return ToolResult(ok=False, display="need a subject", error="subject is required")
        pattern = "%" + subject.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with ctx.pool.acquire() as conn:
            mems = await conn.fetch(
                "SELECT id, layer, content, source, confidence, valid_from, last_verified, "
                "  needs_grounding, scope, claim_key, structured FROM memory "
                "WHERE valid_until IS NULL AND content ILIKE $1 "
                "ORDER BY importance DESC, confidence DESC LIMIT 3", pattern)
            knowledge = [await _audit_one(conn, dict(m)) for m in mems]
            dependent = [r["content"][:140] for r in await conn.fetch(
                "SELECT content FROM memory WHERE layer='procedural'::memory_layer AND valid_until IS NULL "
                "AND (content ILIKE $1 OR structured::text ILIKE $1) LIMIT 5", pattern)]
        connected = await ctx.recall.related(subject) if ctx.recall is not None else {"found": False}
        return ToolResult(
            ok=True,
            output={"subject": subject, "found": bool(knowledge), "knowledge": knowledge,
                    "connected_entities": connected, "dependent_procedures": dependent},
            display=(f"{len(knowledge)} memory(ies) on '{subject}'" if knowledge
                     else f"I have no memory of '{subject}'"))


def register_builtins(registry: Any) -> None:
    registry.register(MemoryAudit())
