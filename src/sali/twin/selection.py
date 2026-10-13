"""Tool selection scoring, alternatives, and chains (spec §21/§53).

Given a task, which of the machine's tools should Sali reach for? This ranks the tools that provide
a needed capability by a deterministic score combining: how reliably the tool has worked HERE (its
learned experience), whether it has actually been used, and its execution authority (prefer the
safer, everyday tool over a privileged/destructive one for the same job). Alternatives fall out of
the capability graph (§21). No model — this is evidence, not opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sali.core.toolvocab import capability_key
from sali.twin.capabilities import capability_slugs_in

# Authority → how much it damps the score. Only a real reason (destructiveness) lowers a tool; the
# freedom philosophy means NORMAL is unpenalised.
_AUTH_PENALTY = {"normal": 0.0, "elevated": 0.3, "system_critical": 1.0}


@dataclass(slots=True)
class ScoredTool:
    name: str
    capability: str
    score: float
    reliability: float | None   # learned success rate on this machine, if known
    used: bool                  # has Sali actually run it?
    authority: str | None


def _score(reliability: float | None, used: bool, authority: str | None) -> float:
    rel = reliability if reliability is not None else 0.5   # unknown → neutral
    auth_pen = _AUTH_PENALTY.get(authority or "normal", 0.0)
    return round(0.5 * rel + 0.2 * (1.0 if used else 0.0) + 0.3 * (1.0 - auth_pen), 3)


async def score_tools(conn: Any, capability: str) -> list[ScoredTool]:
    """The tools that provide ``capability``, ranked best-first."""
    rows = await conn.fetch(
        "SELECT t.name, t.id, "
        "  (SELECT a.authority FROM tool_authority a WHERE a.tool_id=t.id AND a.valid_until IS NULL) AS authority, "
        "  (SELECT m.structured FROM memory m WHERE m.claim_key = 'tool:' || t.name "
        "     AND m.valid_until IS NULL) AS exp "
        "FROM discovered_tool t JOIN graph_edge e ON e.src_id=t.node_id "
        "  JOIN graph_node cap ON cap.id=e.dst_id "
        "WHERE t.available AND e.rel_type='provides_capability' AND e.valid_until IS NULL "
        "  AND cap.canonical_key=$1 AND cap.valid_until IS NULL",
        capability_key(capability))
    scored: list[ScoredTool] = []
    for r in rows:
        exp = r["exp"] or {}
        reliability = exp.get("reliability")
        used = bool(exp)
        scored.append(ScoredTool(
            name=r["name"], capability=capability,
            score=_score(reliability, used, r["authority"]),
            reliability=reliability, used=used, authority=r["authority"]))
    scored.sort(key=lambda s: (-s.score, s.name))
    return scored


async def suggest(conn: Any, task: str, *, limit: int = 8) -> list[ScoredTool]:
    """Rank tools for a free-text task by mapping it to capabilities and scoring their providers.
    A tool that serves several of the asked-for capabilities keeps its best-scoring one."""
    best: dict[str, ScoredTool] = {}
    for slug in capability_slugs_in(task):
        for st in await score_tools(conn, slug):
            prior = best.get(st.name)
            if prior is None or st.score > prior.score:
                best[st.name] = st
    ranked = sorted(best.values(), key=lambda s: (-s.score, s.name))
    return ranked[:limit]


async def alternatives(conn: Any, tool: str, *, limit: int = 8) -> list[str]:
    """Other available tools that share a capability with ``tool`` — its alternatives (§21),
    most-overlapping first."""
    rows = await conn.fetch(
        "SELECT t2.name, count(*) AS shared FROM discovered_tool t1 "
        "  JOIN graph_edge e1 ON e1.src_id=t1.node_id AND e1.rel_type='provides_capability' "
        "    AND e1.valid_until IS NULL "
        "  JOIN graph_edge e2 ON e2.dst_id=e1.dst_id AND e2.rel_type='provides_capability' "
        "    AND e2.valid_until IS NULL "
        "  JOIN discovered_tool t2 ON t2.node_id=e2.src_id AND t2.available AND t2.name <> t1.name "
        "WHERE t1.name=$1 GROUP BY t2.name ORDER BY shared DESC, t2.name LIMIT $2",
        tool, limit)
    return [r["name"] for r in rows]
