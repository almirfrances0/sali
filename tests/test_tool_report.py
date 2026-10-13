"""Tool Intelligence — Increment 8: read-side reporting for the `sali tools` CLI (§25/§63)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.twin import tool_report
from sali.twin.authority import classify_tools
from sali.twin.capabilities import apply_capabilities
from sali.twin.tools import sync_tools

pytestmark = pytest.mark.db


async def _seed(conn: Any, *names: str) -> None:
    await sync_tools(conn, {n: f"/usr/bin/{n}" for n in names}, {n: n for n in names})
    await apply_capabilities(conn)
    await classify_tools(conn)


async def _ran(conn: Any, command: str, *, success: bool = True) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
        "VALUES ($1, 'execute_command', $2, $3, 'observed')",
        uuid4(), {"args": {"command": command}}, success)


async def test_list_tools_and_filters(db_conn: Any) -> None:
    await _seed(db_conn, "nmap", "jq", "mkfs")

    all_rows = {r["name"]: r for r in await tool_report.list_tools(db_conn)}
    assert set(all_rows) == {"nmap", "jq", "mkfs"}
    assert "port_scanning" in all_rows["nmap"]["capabilities"]
    assert all_rows["mkfs"]["authority"] == "system_critical"

    # filter by capability
    scanners = await tool_report.list_tools(db_conn, capability="port_scanning")
    assert [r["name"] for r in scanners] == ["nmap"]
    # filter by authority
    crit = await tool_report.list_tools(db_conn, authority="system_critical")
    assert [r["name"] for r in crit] == ["mkfs"]


async def test_inspect_tool_gathers_everything(db_conn: Any) -> None:
    await _seed(db_conn, "jq")
    from sali.learning.tool_experience import learn_tool_experiences
    for _ in range(3):
        await _ran(db_conn, "jq . file.json", success=True)
    await learn_tool_experiences(db_conn, threshold=3)

    info = await tool_report.inspect_tool(db_conn, "jq")
    assert info is not None
    assert info["package"] == "jq" and info["authority"] == "normal"
    assert "json_processing" in info["capabilities"]
    assert info["experience"]["runs"] == 3 and info["experience"]["reliability"] == 1.0
    assert "jq" in (info["experience_summary"] or "")


async def test_inspect_unknown_tool_is_none(db_conn: Any) -> None:
    assert await tool_report.inspect_tool(db_conn, "does-not-exist") is None


async def test_coverage_counts_discovered_mapped_classified_used(db_conn: Any) -> None:
    await _seed(db_conn, "nmap", "jq", "mkfs", "obscure-tool")
    await _ran(db_conn, "jq . a.json")
    await _ran(db_conn, "jq . b.json")
    await _ran(db_conn, "nmap -sV host")

    cov = await tool_report.coverage(db_conn)
    assert cov["discovered"] == 4
    assert cov["with_capability"] == 2          # nmap + jq have capability rules; mkfs/obscure don't
    assert cov["classified"] == 4               # all get an authority
    assert cov["by_authority"]["system_critical"] == 1
    assert cov["used"] == 2 and cov["never_used"] == 2   # only jq + nmap were run
    assert dict(cov["top_used"])["jq"] == 2
