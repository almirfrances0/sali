"""Architecture review · Increment 9 — richer procedure shape (§36/§4).

A learned procedure now carries its tools, required-tools precondition, known failure modes (joined
from the tools' learned experiences), and a verification hint — so a recalled procedure guides the next
turn on what breaks and how to confirm, not just the raw step sequence.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.learning.procedures import learn_procedures
from sali.memory import writer
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


async def _ran(conn: Any, run_id: Any, command: str) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
        "VALUES ($1,'execute_command',$2,true,'observed')",
        run_id, {"args": {"command": command}})


async def test_learned_procedure_carries_tools_failuremodes_verification(db_conn: Any) -> None:
    # two runs of the same docker deploy sequence → clears the evidence bar
    for _ in range(2):
        run = uuid4()
        await _ran(db_conn, run, "docker compose build")
        await _ran(db_conn, run, "docker compose up -d")
    # a learned tool experience for docker with a known failure mode (data already on hand)
    await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC, content="docker: 90% success",
                          source=MemorySource.INFERENCE, functional=True, claim_key="tool:docker",
                          structured={"failure_modes": ["port already allocated"], "certainty": "learned"})

    named = FakeModelProvider(responses=[ChatResult("Docker deploy", None, [], 3, 3, "fake")])
    learned = await learn_procedures(db_conn, named, threshold=2)

    assert learned
    row = await db_conn.fetchrow(
        "SELECT structured FROM memory WHERE layer='procedural' AND valid_until IS NULL "
        "AND structured->>'name'='Docker deploy'")
    s = row["structured"]
    assert s["tools"] == ["docker"] and s["requires_tools"] == ["docker"]  # precondition
    assert "port already allocated" in s["known_failure_modes"]            # joined from tool experience
    assert s["verification"] == "docker compose up"                       # the last step, normalized
    assert s["certainty"] == "learned"
