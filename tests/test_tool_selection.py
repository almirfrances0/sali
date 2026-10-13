"""Tool Intelligence — Increment 9: selection scoring, alternatives, and the advice tools (§21/§53)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.learning.tool_experience import learn_tool_experiences
from sali.tools.builtins.tool_advice import RecommendTool, ToolAlternatives
from sali.tools.context import ToolContext
from sali.twin import selection
from sali.twin.authority import classify_tools
from sali.twin.capabilities import apply_capabilities
from sali.twin.tools import sync_tools

pytestmark = pytest.mark.db


async def _seed(conn: Any, *names: str) -> None:
    await sync_tools(conn, {n: f"/usr/bin/{n}" for n in names}, {})
    await apply_capabilities(conn)
    await classify_tools(conn)


async def _ran(conn: Any, command: str, *, success: bool = True) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
        "VALUES ($1, 'execute_command', $2, $3, 'observed')",
        uuid4(), {"args": {"command": command}}, success)


async def test_score_prefers_the_proven_reliable_tool(db_conn: Any) -> None:
    await _seed(db_conn, "nmap", "masscan")  # both provide port_scanning
    for _ in range(4):  # nmap has a track record here; masscan doesn't
        await _ran(db_conn, "nmap -sV host", success=True)
    await learn_tool_experiences(db_conn, threshold=3)

    ranked = await selection.score_tools(db_conn, "port_scanning")
    assert [s.name for s in ranked][0] == "nmap"        # proven + used → ranked first
    top = ranked[0]
    assert top.used is True and top.reliability == 1.0
    assert next(s for s in ranked if s.name == "masscan").used is False


async def test_suggest_maps_a_task_to_ranked_tools(db_conn: Any) -> None:
    await _seed(db_conn, "tcpdump", "jq")
    ranked = await selection.suggest(db_conn, "I need to capture packets on the network")
    assert ranked and ranked[0].name == "tcpdump"
    assert all(s.name != "jq" for s in ranked)  # jq isn't relevant to packet capture


async def test_alternatives_come_from_shared_capabilities(db_conn: Any) -> None:
    await _seed(db_conn, "nmap", "masscan", "jq")
    alts = await selection.alternatives(db_conn, "nmap")
    assert "masscan" in alts and "jq" not in alts and "nmap" not in alts


async def test_recommend_tool_runs_through_the_catalog_sink(db_conn: Any) -> None:
    await _seed(db_conn, "tcpdump")

    class _Catalog:
        async def suggest(self, task: str) -> list[dict[str, Any]]:
            return [{"tool": s.name, "capability": s.capability, "score": s.score,
                     "reliability": s.reliability, "used": s.used, "authority": s.authority}
                    for s in await selection.suggest(db_conn, task)]

        async def alternatives(self, tool: str) -> list[str]:
            return await selection.alternatives(db_conn, tool)

    ctx = ToolContext(settings=Settings(), clock=SystemClock(), catalog=_Catalog())
    res = await RecommendTool().run({"task": "capture packets"}, ctx)
    assert res.ok and res.output["tools"][0]["tool"] == "tcpdump"

    alt = await ToolAlternatives().run({"tool": "tcpdump"}, ctx)
    assert alt.ok  # no alternatives seeded, but the call succeeds cleanly
