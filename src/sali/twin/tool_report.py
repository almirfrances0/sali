"""Read-side reporting for the Tool-Intelligence CLI (spec §25/§63).

Plain data functions the `sali tools` command renders — kept out of the CLI so they're testable.
Everything reads the discovered_tool inventory, the capability/authority records, the graph, and the
learned tool:<binary> experiences; nothing here writes or executes.
"""

from __future__ import annotations

from typing import Any

from sali.core.toolvocab import NODE_CAPABILITY, binary_of, capability_key


async def list_tools(
    conn: Any, *, capability: str | None = None, authority: str | None = None,
    available_only: bool = True, limit: int = 200,
) -> list[dict[str, Any]]:
    """Discovered tools with their authority tier and capability slugs, optionally filtered."""
    clauses = ["1=1"]
    params: list[Any] = []
    if available_only:
        clauses.append("t.available")
    if authority is not None:
        params.append(authority)
        clauses.append(f"cur.authority = ${len(params)}")
    if capability is not None:
        params.append(capability_key(capability))
        clauses.append(
            f"EXISTS (SELECT 1 FROM graph_edge e JOIN graph_node cap ON cap.id=e.dst_id "
            f"  WHERE e.src_id=t.node_id AND e.rel_type='provides_capability' AND e.valid_until IS NULL "
            f"  AND cap.canonical_key=${len(params)} AND cap.valid_until IS NULL)")
    params.append(limit)
    rows = await conn.fetch(
        "SELECT t.name, t.path, t.package, cur.authority, "
        "  ARRAY(SELECT replace(cap.canonical_key, 'capability:', '') "
        "        FROM graph_edge e JOIN graph_node cap ON cap.id=e.dst_id "
        "        WHERE e.src_id=t.node_id AND e.rel_type='provides_capability' "
        "          AND e.valid_until IS NULL AND cap.valid_until IS NULL ORDER BY 1) AS capabilities "
        "FROM discovered_tool t "
        "LEFT JOIN tool_authority cur ON cur.tool_id=t.id AND cur.valid_until IS NULL "
        f"WHERE {' AND '.join(clauses)} ORDER BY t.name LIMIT ${len(params)}",
        *params)
    return [dict(r) for r in rows]


async def inspect_tool(conn: Any, name: str) -> dict[str, Any] | None:
    """Everything Sali knows about one tool: inventory, authority (+rationale), capabilities, and its
    learned experience. None if the tool isn't in the inventory."""
    t = await conn.fetchrow(
        "SELECT id, name, path, package, current_version, available, node_id, first_seen, last_seen "
        "FROM discovered_tool WHERE name=$1", name)
    if t is None:
        return None
    auth = await conn.fetchrow(
        "SELECT authority, rationale, classifier, capabilities FROM tool_authority "
        "WHERE tool_id=$1 AND valid_until IS NULL", t["id"])
    caps = [r["slug"] for r in await conn.fetch(
        "SELECT replace(cap.canonical_key, 'capability:', '') AS slug "
        "FROM graph_edge e JOIN graph_node cap ON cap.id=e.dst_id "
        "WHERE e.src_id=$1 AND e.rel_type='provides_capability' AND e.valid_until IS NULL "
        "  AND cap.valid_until IS NULL ORDER BY 1", t["node_id"])]
    experience = await conn.fetchrow(
        "SELECT content, structured FROM memory WHERE claim_key=$1 AND valid_until IS NULL",
        f"tool:{name}")
    return {
        "name": t["name"], "path": t["path"], "package": t["package"],
        "version": t["current_version"], "available": t["available"],
        "first_seen": t["first_seen"], "last_seen": t["last_seen"],
        "authority": auth["authority"] if auth else None,
        "authority_rationale": auth["rationale"] if auth else None,
        "authority_classifier": auth["classifier"] if auth else None,
        "capabilities": caps,
        "experience": (dict(experience["structured"]) if experience and experience["structured"]
                       else None),
        "experience_summary": experience["content"] if experience else None,
    }


async def coverage(conn: Any, *, usage_scan_limit: int = 5000) -> dict[str, Any]:
    """Tool-knowledge coverage (§25): discovered vs mapped vs classified vs actually-used."""
    discovered = await conn.fetchval("SELECT count(*) FROM discovered_tool WHERE available")
    with_capability = await conn.fetchval(
        "SELECT count(DISTINCT t.id) FROM discovered_tool t "
        "JOIN graph_edge e ON e.src_id=t.node_id "
        "WHERE t.available AND e.rel_type='provides_capability' AND e.valid_until IS NULL")
    by_authority = {
        r["authority"]: r["n"] for r in await conn.fetch(
            "SELECT a.authority, count(*) AS n FROM tool_authority a "
            "JOIN discovered_tool t ON t.id=a.tool_id "
            "WHERE a.valid_until IS NULL AND t.available GROUP BY a.authority")}
    classified = sum(by_authority.values())
    capabilities = await conn.fetchval(
        "SELECT count(*) FROM graph_node WHERE node_type=$1 AND valid_until IS NULL", NODE_CAPABILITY)

    # Which discovered tools have Sali actually run? Scan recent command executions for their binary.
    names = {r["name"] for r in await conn.fetch(
        "SELECT name FROM discovered_tool WHERE available")}
    commands = await conn.fetch(
        "SELECT plan->'args'->>'command' AS command FROM tool_execution "
        "WHERE tool_name='execute_command' AND plan->'args'->>'command' IS NOT NULL "
        "ORDER BY started_at DESC LIMIT $1", usage_scan_limit)
    used_counts: dict[str, int] = {}
    for r in commands:
        b = binary_of(str(r["command"]))
        if b in names:
            used_counts[b] = used_counts.get(b, 0) + 1
    used = len(used_counts)
    top_used = sorted(used_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {
        "discovered": discovered, "capabilities": capabilities,
        "with_capability": with_capability, "classified": classified, "by_authority": by_authority,
        "used": used, "never_used": max(0, discovered - used), "top_used": top_used,
    }
