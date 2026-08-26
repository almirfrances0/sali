"""Active memory recall — the tools that let Sali QUERY its own memory, not just receive it (§34,§56).

Sali already gets the most relevant memories injected before a turn. These tools let it dig deeper on
its own initiative: search for a specific fact or past experience, walk the knowledge graph around an
entity, pull a learned procedure or a past incident, or trace how a fact changed over time. Every
result carries provenance + confidence + a staleness flag, so Sali reasons over evidence and can say
"I don't have a reliable memory of that" instead of inventing one (§47). All read-only.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_MAX = 12 * 1024


class _Recall(Tool):
    """Shared base: read-only, benign, always available."""

    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.READ})
    idempotent = True


class MemorySearch(_Recall):
    name = "memory_search"
    description = (
        "Search your own memory for facts, notes, and past experiences relevant to a query — use it "
        "whenever the answer might depend on something you learned before (a past decision, a project "
        "detail, something Almir told you). Returns matching memories with their source, confidence, "
        "and whether they may be stale. If nothing relevant comes back, you have no memory of it — "
        "say so rather than guessing."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look for."}},
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory recall isn't available")
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, display="need a query", error="query is required")
        hits = await ctx.recall.search(query, k=6)
        return _result(hits, f"searched memory ({len(hits)})", "no memory matches that")


class MemoryRecallProcedure(_Recall):
    name = "memory_recall_procedure"
    description = (
        "Look up a learned procedure — a how-to Sali worked out before (e.g. 'deploy project X', "
        "'fix the GPU issue'). Prefer a remembered procedure that worked over improvising. Returns "
        "the steps/notes with confidence; empty means you haven't learned one yet."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The task / how-to to look up."}},
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory recall isn't available")
        query = str(args.get("query", "")).strip()
        hits = await ctx.recall.search(query, layer="procedural", k=5)
        return _result(hits, f"recalled procedures ({len(hits)})", "no learned procedure for that yet")


class MemoryRecallIncident(_Recall):
    name = "memory_recall_incident"
    description = (
        "Look up past failures/incidents similar to what's happening now — BEFORE re-solving a problem "
        "from scratch. If Sali hit this before, recall what the cause and fix turned out to be — then "
        "VERIFY against the current machine rather than blindly repeating it (§24). Empty means no "
        "prior incident on record."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The problem / symptom."}},
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory recall isn't available")
        query = str(args.get("query", "")).strip()
        hits = await ctx.recall.search(query, layer="episodic", k=5)
        return _result(hits, f"recalled incidents ({len(hits)})", "no similar incident on record")


class MemoryRelated(_Recall):
    name = "memory_related"
    description = (
        "Find what's connected to an entity in your knowledge graph — its relationships (what it "
        "runs, depends on, belongs to, is hosted on…). Use it to reason about Almir's setup: 'what's "
        "related to my VPS', 'what does project X use'. Set hops>1 to walk further out. Empty means "
        "you don't know that entity yet."
    )
    parameters = {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "The thing to look around (a name/surface form)."},
            "hops": {"type": "integer", "description": "How many relationship hops to walk (default 1)."},
        },
        "required": ["entity"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory recall isn't available")
        entity = str(args.get("entity", "")).strip()
        hops = max(1, min(int(args.get("hops") or 1), 4))
        found = await ctx.recall.related(entity, hops=hops)
        if not found.get("found"):
            return ToolResult(ok=True, output=found, display=f"nothing known about '{entity}'")
        return ToolResult(ok=True, output=_cap(found),
                          display=f"{found['resolved']}: {len(found['relations'])} relations")


class MemoryEntity(_Recall):
    name = "memory_entity"
    description = (
        "Get what you know about a specific entity — its type, properties, and current relationships "
        "(e.g. 'the VPS', 'Ollama', 'project X'). A focused snapshot of one thing. Empty means "
        "unknown."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The entity name / surface form."}},
        "required": ["name"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory recall isn't available")
        name = str(args.get("name", "")).strip()
        snap = await ctx.recall.entity(name)
        if not snap.get("found"):
            return ToolResult(ok=True, output=snap, display=f"nothing known about '{name}'")
        return ToolResult(ok=True, output=_cap(snap), display=f"what I know about {snap['name']}")


class MemoryHistory(_Recall):
    name = "memory_history"
    description = (
        "Trace how a fact changed over time — the timeline of a relationship, current and past (e.g. "
        "which Ollama version was installed last week, what database project X used before). Give the "
        "entity and the relationship (e.g. entity='Ollama', relation='has_version'). Empty means no "
        "recorded history."
    )
    parameters = {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "The entity whose history you want."},
            "relation": {"type": "string", "description": "The relationship/slot, e.g. 'has_version', 'uses'."},
        },
        "required": ["entity", "relation"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory recall isn't available")
        entity = str(args.get("entity", "")).strip()
        relation = str(args.get("relation", "")).strip()
        if not entity or not relation:
            return ToolResult(ok=False, display="need entity + relation",
                              error="entity and relation are required")
        hist = await ctx.recall.history(entity, relation)
        if not hist.get("found"):
            return ToolResult(ok=True, output=hist, display=f"no recorded history for {entity} {relation}")
        return ToolResult(ok=True, output=_cap(hist),
                          display=f"{hist['entity']} {hist['relation']}: {len(hist['timeline'])} entries")


def _result(hits: list[dict[str, Any]], ok_display: str, empty: str) -> ToolResult:
    if not hits:
        return ToolResult(ok=True, output={"matches": []}, display=empty)
    return ToolResult(ok=True, output=_cap({"matches": hits}), display=ok_display)


def _cap(obj: dict[str, Any]) -> dict[str, Any]:
    import json

    return obj if len(json.dumps(obj)) <= _MAX else {**obj, "_truncated": True}


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (MemorySearch(), MemoryRecallProcedure(), MemoryRecallIncident(),
                 MemoryRelated(), MemoryEntity(), MemoryHistory()):
        registry.register(tool)
