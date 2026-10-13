"""Tool Intelligence — Increment 4: deterministic execution-authority classification (§34/§35).

Record-only: every tool gets a NORMAL/ELEVATED/SYSTEM_CRITICAL tier written immutably. Reclassification
supersedes (never overwrites); the classification is idempotent; nothing is gated yet.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import Capability, ExecAuthority, MemorySource
from sali.twin import tools
from sali.twin.authority import classify_authority, classify_tools, current_authority

pytestmark = pytest.mark.db


def test_classify_authority_tiers() -> None:
    assert classify_authority("mkfs")[0] is ExecAuthority.SYSTEM_CRITICAL
    assert classify_authority("fdisk")[0] is ExecAuthority.SYSTEM_CRITICAL
    assert classify_authority("reboot")[0] is ExecAuthority.SYSTEM_CRITICAL
    assert classify_authority("apt")[0] is ExecAuthority.ELEVATED
    assert classify_authority("systemctl")[0] is ExecAuthority.ELEVATED
    assert classify_authority("ls")[0] is ExecAuthority.NORMAL
    assert classify_authority("jq")[0] is ExecAuthority.NORMAL
    assert all(classify_authority(n)[1] for n in ("mkfs", "apt", "ls"))  # rationale always present


async def _seed(conn: Any, *names: str) -> None:
    await tools.sync_tools(conn, {n: f"/usr/bin/{n}" for n in names}, {})


async def test_classify_tools_records_authority_with_capabilities(db_conn: Any) -> None:
    await _seed(db_conn, "mkfs", "apt", "ls")

    result = await classify_tools(db_conn)

    assert result.classified == 3 and result.written == 3
    assert result.system_critical == 1 and result.elevated == 1 and result.normal == 1
    assert await current_authority(db_conn, "mkfs") is ExecAuthority.SYSTEM_CRITICAL
    assert await current_authority(db_conn, "apt") is ExecAuthority.ELEVATED
    assert await current_authority(db_conn, "ls") is ExecAuthority.NORMAL
    # SYSTEM_CRITICAL records the immutable-boundary capabilities
    caps = await db_conn.fetchval(
        "SELECT a.capabilities FROM tool_authority a JOIN discovered_tool t ON t.id=a.tool_id "
        "WHERE t.name='mkfs' AND a.valid_until IS NULL")
    assert Capability.SYSTEM.value in caps and Capability.DESTRUCTIVE.value in caps


async def test_classification_is_idempotent(db_conn: Any) -> None:
    await _seed(db_conn, "apt", "ls")
    first = await classify_tools(db_conn)
    second = await classify_tools(db_conn)

    assert first.written == 2 and second.written == 0  # nothing to rewrite the second time
    apt_id = await db_conn.fetchval("SELECT id FROM discovered_tool WHERE name='apt'")
    assert await db_conn.fetchval(
        "SELECT count(*) FROM tool_authority WHERE tool_id=$1", apt_id) == 1  # no duplicate rows


async def test_reclassification_supersedes_never_overwrites(db_conn: Any) -> None:
    await _seed(db_conn, "ls")
    ls_id = await db_conn.fetchval("SELECT id FROM discovered_tool WHERE name='ls'")
    # a stale/wrong classification already on record
    await db_conn.execute(
        "INSERT INTO tool_authority (tool_id, authority, source) VALUES ($1,'system_critical',$2::memory_source)",
        ls_id, MemorySource.INFERENCE.value)

    await classify_tools(db_conn)

    assert await current_authority(db_conn, "ls") is ExecAuthority.NORMAL  # corrected
    rows = await db_conn.fetch(
        "SELECT authority, valid_until FROM tool_authority WHERE tool_id=$1 ORDER BY created_at", ls_id)
    assert len(rows) == 2  # history retained
    assert rows[0]["authority"] == "system_critical" and rows[0]["valid_until"] is not None  # closed
    assert rows[1]["authority"] == "normal" and rows[1]["valid_until"] is None  # current
