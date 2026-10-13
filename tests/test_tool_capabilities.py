"""Tool Intelligence — Increment 3: deterministic capability mapping (§6/§7/§21).

Known tools get provides_capability edges to seeded capability nodes; unknown/absent tools honestly
get none; "which tools provide X" resolves structurally; the rules stay inside the vocabulary.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.toolvocab import (
    CAPABILITY_VOCAB,
    REL_PROVIDES_CAPABILITY,
    capability_key,
    ext_tool_key,
)
from sali.twin import tools
from sali.twin.capabilities import CAPABILITY_RULES, apply_capabilities, tools_with_capability

pytestmark = pytest.mark.db


def test_all_rule_slugs_are_in_the_vocabulary() -> None:
    for binary, slugs in CAPABILITY_RULES.items():
        for slug in slugs:
            assert slug in CAPABILITY_VOCAB, f"{binary} maps to unknown capability {slug!r}"


async def _seed(conn: Any, *names: str) -> None:
    await tools.sync_tools(conn, {n: f"/usr/bin/{n}" for n in names}, {})


async def test_known_tools_get_capabilities_unknown_do_not(db_conn: Any) -> None:
    await _seed(db_conn, "nmap", "jq", "frobnicate")  # frobnicate has no rule

    result = await apply_capabilities(db_conn)

    assert result.capabilities == len(CAPABILITY_VOCAB)
    # nmap → its three capabilities
    async def _caps(name: str) -> set[str]:
        rows = await db_conn.fetch(
            "SELECT cap.canonical_key FROM graph_edge e "
            "  JOIN graph_node cap ON cap.id=e.dst_id "
            "  JOIN graph_node tool ON tool.id=e.src_id "
            "WHERE e.rel_type=$1 AND e.valid_until IS NULL AND tool.canonical_key=$2",
            REL_PROVIDES_CAPABILITY, ext_tool_key(name))
        return {r["canonical_key"] for r in rows}

    assert await _caps("nmap") == {
        capability_key("host_discovery"), capability_key("port_scanning"),
        capability_key("service_detection")}
    assert await _caps("jq") == {capability_key("json_processing")}
    assert await _caps("frobnicate") == set()  # honest gap — no invented capability


async def test_absent_tool_gets_no_edge_even_if_it_has_a_rule(db_conn: Any) -> None:
    await _seed(db_conn, "jq")  # nmap has a rule but is NOT installed here
    await apply_capabilities(db_conn)
    assert await tools_with_capability(db_conn, "port_scanning") == []  # nmap absent → nothing


async def test_tools_with_capability_resolves_structurally(db_conn: Any) -> None:
    await _seed(db_conn, "nmap", "masscan", "jq")
    await apply_capabilities(db_conn)
    # both nmap and masscan provide port_scanning → they are alternatives (§21)
    assert await tools_with_capability(db_conn, "port_scanning") == ["masscan", "nmap"]
    assert await tools_with_capability(db_conn, "json_processing") == ["jq"]


async def test_apply_is_idempotent(db_conn: Any) -> None:
    await _seed(db_conn, "git")
    await apply_capabilities(db_conn)
    await apply_capabilities(db_conn)
    edges = await db_conn.fetchval(
        "SELECT count(*) FROM graph_edge e JOIN graph_node tool ON tool.id=e.src_id "
        "WHERE e.rel_type=$1 AND e.valid_until IS NULL AND tool.canonical_key=$2",
        REL_PROVIDES_CAPABILITY, ext_tool_key("git"))
    assert edges == 1  # version_control, exactly once
