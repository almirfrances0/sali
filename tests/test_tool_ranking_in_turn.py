"""Architecture review · Increment 5 — reliability-ranked tool surfacing in-turn (§3/§8).

The learned tool experience (reliability/used) now orders the tools Sali surfaces in a turn's prompt,
not just the advice CLI — so memory changes which tool Sali reaches for, everywhere.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.provider.fake import FakeModelProvider
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.twin import tools
from sali.twin.authority import classify_tools
from sali.twin.capabilities import apply_capabilities

pytestmark = pytest.mark.db


async def _seed(conn: Any, *names: str) -> None:
    await tools.sync_tools(conn, {n: f"/usr/bin/{n}" for n in names}, {})
    await apply_capabilities(conn)
    await classify_tools(conn)


async def _ran(conn: Any, command: str, *, success: bool) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
        "VALUES ($1,'execute_command',$2,$3,'observed')",
        uuid4(), {"args": {"command": command}}, success)


async def test_tool_facts_are_reliability_ranked(live_pool: Any) -> None:
    from sali.learning.tool_experience import learn_tool_experiences

    async with live_pool.acquire() as conn:
        await _seed(conn, "nmap", "masscan")           # both provide port_scanning
        for _ in range(4):                              # nmap has a clean track record here
            await _ran(conn, "nmap -sV host", success=True)
        for _ in range(3):                              # masscan has been failing
            await _ran(conn, "masscan 10.0.0.0/8", success=False)
        await learn_tool_experiences(conn, threshold=3)

    svc = RetrievalService(live_pool, FakeModelProvider())
    q = "which tool for port scanning"
    bundle = await svc.gather(q, classify(q))
    port = next(tf for tf in bundle.tool_facts if tf.capability == "port_scanning")
    assert port.tools[0] == "nmap"  # the reliable, proven tool ranks first (was alphabetical: masscan)
