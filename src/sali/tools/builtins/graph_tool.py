"""The relate tool — Sali records how things connect (spec §7-8, §41).

When Almir states a relationship ("Salix Studio is deployed on VPS-01", "I develop Sali"), it should
become an edge in the knowledge graph, not just loose text — so later "what's deployed on VPS-01?"
answers from structure. The edge carries honest provenance (CONVERSATION) and confidence; the graph
writer handles temporal validity and contradictions. Explicit and verified, like remember().
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, MemorySource, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class Relate(Tool):
    name = "relate"
    description = (
        "Record how two things connect in your knowledge graph, so you can answer questions about "
        "them later. Give subject, relation, object — e.g. subject 'Salix Studio', relation "
        "'deployed_on', object 'VPS-01'. Use it when Almir tells you how things relate."
    )
    parameters = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "The thing the relationship starts from."},
            "relation": {"type": "string", "description": "How they relate, e.g. 'deployed_on', 'develops'."},
            "object": {"type": "string", "description": "The thing the relationship points to."},
        },
        "required": ["subject", "relation", "object"],
    }
    risk_level = RiskLevel.R1  # benign: Sali writing to its own knowledge graph
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        subject = str(args.get("subject", "")).strip()
        relation = str(args.get("relation", "")).strip()
        obj = str(args.get("object", "")).strip()
        if not (subject and relation and obj):
            return ToolResult(ok=False, display="need subject, relation, object",
                              error="subject, relation and object are all required")
        if ctx.graph is None:
            return ToolResult(ok=False, display="no graph", error="the graph isn't available right now")

        await ctx.graph.link(
            subject=subject, relation=relation, obj=obj, source=MemorySource.CONVERSATION,
        )
        return ToolResult(
            ok=True,
            output={"subject": subject, "relation": relation, "object": obj},
            display=f"linked {subject} —{relation}→ {obj}",
        )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(Relate())
