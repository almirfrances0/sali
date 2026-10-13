"""self_diagnostics — Sali checks its own health AND the consistency of what it knows (spec §9).

Two parts. SUBSYSTEM health (model, datastore, embedder, perception, internet — via HealthService) plus
memory/graph/attention liveness metrics. And — the part that matters most for trust — KNOWLEDGE
CONSISTENCY WARNINGS derived from the real stores: unresolved contradictions, a functional claim that
somehow has two current values, an identity that doesn't resolve to the canonical agent, and graph facts
the filesystem contradicts (a location the graph records but disk says is absent). It turns "something
feels off" into concrete, grounded findings. Read-only.
"""

from __future__ import annotations

import os
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext


async def consistency_warnings(conn: Any) -> list[str]:
    """Concrete knowledge-consistency problems found in the real stores (empty = consistent)."""
    warnings: list[str] = []

    open_contradictions = await conn.fetchval(
        "SELECT count(*) FROM contradiction WHERE status='open'")
    if open_contradictions:
        warnings.append(f"{open_contradictions} unresolved contradiction(s) in memory — "
                        "facts that disagree and haven't been settled by evidence")

    # A functional claim must have exactly one current value; more than one is an integrity fault.
    for r in await conn.fetch(
            "SELECT claim_key, count(*) AS c FROM memory WHERE valid_until IS NULL "
            "AND claim_key IS NOT NULL GROUP BY claim_key HAVING count(*) > 1"):
        warnings.append(f"claim '{r['claim_key']}' has {r['c']} conflicting CURRENT values (should be 1)")

    # Graph vs filesystem: a location the graph records but disk contradicts.
    for r in await conn.fetch(
            "SELECT name, props FROM graph_node WHERE node_type='location' AND valid_until IS NULL"):
        path = str((r["props"] or {}).get("path", ""))
        if path and not os.path.exists(path):
            warnings.append(f"graph says '{r['name']}' is at {path}, but the filesystem disagrees (absent)")

    return warnings


async def _metrics(conn: Any) -> dict[str, Any]:
    async def one(sql: str) -> int:
        return int(await conn.fetchval(sql) or 0)

    return {
        "memories_current": await one("SELECT count(*) FROM memory WHERE valid_until IS NULL"),
        "procedures": await one("SELECT count(*) FROM memory WHERE layer='procedural' AND valid_until IS NULL"),
        "episodes": await one("SELECT count(*) FROM memory WHERE layer='episodic' AND valid_until IS NULL"),
        "graph_nodes": await one("SELECT count(*) FROM graph_node WHERE valid_until IS NULL"),
        "graph_edges": await one("SELECT count(*) FROM graph_edge WHERE valid_until IS NULL"),
        "discovered_tools": await one("SELECT count(*) FROM discovered_tool WHERE available"),
        "learning_queue_pending": await one("SELECT count(*) FROM learning_queue WHERE status='pending'"),
        "recent_observations": await one(
            "SELECT count(*) FROM event WHERE event_type='desktop.observed' "
            "AND created_at > now() - interval '1 hour'"),
    }


class SelfDiagnostics(Tool):
    name = "self_diagnostics"
    description = (
        "Run a full self-diagnostic: the health of each subsystem (model, database, memory, graph, "
        "perception, internet) plus KNOWLEDGE CONSISTENCY WARNINGS — unresolved contradictions, memories "
        "that disagree, an identity that resolves ambiguously, or a graph fact the filesystem contradicts. "
        "Use it when Almir asks if you're OK, or when something seems inconsistent."
    )
    parameters = {"type": "object", "properties": {}}
    risk_level = RiskLevel.R0
    capabilities = frozenset({Capability.READ})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.pool is None:
            return ToolResult(ok=False, display="no datastore", error="diagnostics aren't available")
        report: dict[str, Any] = {}
        if ctx.health is not None:
            report["subsystems"] = await ctx.health.report()
        async with ctx.pool.acquire() as conn:
            report["metrics"] = await _metrics(conn)
            warnings = await consistency_warnings(conn)

        # Identity: does the canonical name resolve to the agent, and how ambiguous is it? (§5)
        if ctx.recall is not None:
            ident = await ctx.recall.related("Sali")
            resolved = ident.get("resolved") if ident.get("found") else None
            report["identity"] = {
                "resolves_to": resolved.get("canonical_key") if resolved else None,
                "ambiguous": ident.get("ambiguous", False),
                "other_candidates": len(ident.get("other_candidates", [])),
            }
            if resolved and resolved.get("canonical_key") != "agent:sali":
                warnings.append(
                    f"identity 'Sali' resolves to {resolved.get('canonical_key')}, not agent:sali")

        report["warnings"] = warnings
        degraded = (report.get("subsystems") or {}).get("degraded") or []
        report["healthy"] = not warnings and not degraded
        display = ("all healthy, no consistency warnings" if report["healthy"]
                   else f"{len(warnings)} warning(s), {len(degraded)} degraded subsystem(s)")
        return ToolResult(ok=True, output=report, display=display)


def register_builtins(registry: Any) -> None:
    registry.register(SelfDiagnostics())
