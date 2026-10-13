"""Tool Intelligence — Increment 1: schema + vocabulary substrate (§3/§34/§75).

Pure foundation: the discovered_tool inventory + immutable tool_authority record, the ExecAuthority
enum kept in lockstep with the SQL CHECK, its mapping onto the existing RiskLevel/Capability gate,
and the canonical graph vocabulary. No behavior yet — these guard the substrate the later
increments build on.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from sali.core import toolvocab
from sali.core.enums import (
    Capability,
    ExecAuthority,
    MemorySource,
    RiskLevel,
    authority_capabilities,
    authority_risk,
)

pytestmark = pytest.mark.db


# ---- enum ↔ policy mapping (no DB) -----------------------------------------------------------

def test_authority_risk_puts_only_system_critical_at_the_gated_level() -> None:
    # The freedom policy auto-allows R0–R3 and gates only R4, so SYSTEM_CRITICAL must be the ONLY
    # tier that reaches the immutable boundary; NORMAL/ELEVATED stay autonomous.
    assert authority_risk(ExecAuthority.NORMAL) == RiskLevel.R2
    assert authority_risk(ExecAuthority.ELEVATED) == RiskLevel.R3
    assert authority_risk(ExecAuthority.SYSTEM_CRITICAL) == RiskLevel.R4
    gated = {a for a in ExecAuthority if authority_risk(a) >= RiskLevel.R4}
    assert gated == {ExecAuthority.SYSTEM_CRITICAL}


def test_system_critical_activates_the_immutable_boundary_capabilities() -> None:
    normal = authority_capabilities(ExecAuthority.NORMAL)
    elevated = authority_capabilities(ExecAuthority.ELEVATED)
    critical = authority_capabilities(ExecAuthority.SYSTEM_CRITICAL)

    assert normal == frozenset({Capability.EXECUTE})
    assert Capability.SYSTEM in elevated and Capability.DESTRUCTIVE not in elevated
    # SYSTEM_CRITICAL lights up the (previously dead) SYSTEM capability + DESTRUCTIVE — the anchors
    # the policy floors key on.
    assert {Capability.SYSTEM, Capability.DESTRUCTIVE, Capability.EXECUTE} <= critical


# ---- vocabulary (no DB) ----------------------------------------------------------------------

def test_canonical_keys_are_stable_and_prefixed() -> None:
    assert toolvocab.ext_tool_key("nmap") == "ext_tool:nmap"
    assert toolvocab.capability_key("host_discovery") == "capability:host_discovery"
    assert toolvocab.os_package_key("nmap") == "pkg:nmap"
    assert toolvocab.NODE_EXT_TOOL == "ext_tool"
    assert toolvocab.REL_PROVIDES_CAPABILITY == "provides_capability"


def test_capability_vocab_is_well_formed() -> None:
    assert toolvocab.CAPABILITY_VOCAB, "the vocabulary must not be empty"
    for slug, desc in toolvocab.CAPABILITY_VOCAB.items():
        assert slug == slug.lower() and " " not in slug, f"slug not canonical: {slug!r}"
        assert slug.replace("_", "").isalnum(), f"slug not snake_case: {slug!r}"
        assert desc.strip(), f"empty description for {slug!r}"
    assert toolvocab.is_known_capability("host_discovery")
    assert not toolvocab.is_known_capability("not_a_real_capability")


# ---- schema round-trips (DB) -----------------------------------------------------------------

async def _insert_tool(conn: Any, name: str = "nmap") -> Any:
    return await conn.fetchval(
        "INSERT INTO discovered_tool (name, path, package, current_version, source) "
        "VALUES ($1,$2,$3,$4,$5::memory_source) RETURNING id",
        name, f"/usr/bin/{name}", name, "7.94", MemorySource.SYSTEM_OBSERVATION.value)


async def test_discovered_tool_round_trips(db_conn: Any) -> None:
    tool_id = await _insert_tool(db_conn)
    row = await db_conn.fetchrow("SELECT * FROM discovered_tool WHERE id=$1", tool_id)
    assert row["name"] == "nmap" and row["available"] is True
    assert row["path"] == "/usr/bin/nmap" and row["package"] == "nmap"
    assert row["source"] == "system_observation" and 0.0 <= row["confidence"] <= 1.0


async def test_discovered_tool_name_is_unique(db_conn: Any) -> None:
    await _insert_tool(db_conn, "ripgrep")
    with pytest.raises(asyncpg.UniqueViolationError):  # one stable row per tool name
        async with db_conn.transaction():
            await _insert_tool(db_conn, "ripgrep")


async def test_tool_authority_check_matches_the_python_enum(db_conn: Any) -> None:
    tool_id = await _insert_tool(db_conn)
    # every ExecAuthority value is accepted by the SQL CHECK (lockstep)
    for auth in ExecAuthority:
        await db_conn.execute(
            "UPDATE tool_authority SET valid_until=now() WHERE tool_id=$1 AND valid_until IS NULL",
            tool_id)
        await db_conn.execute(
            "INSERT INTO tool_authority (tool_id, authority, capabilities, source) "
            "VALUES ($1,$2,$3,$4::memory_source)",
            tool_id, auth.value, [c.value for c in authority_capabilities(auth)],
            MemorySource.INFERENCE.value)
    # an unknown tier is rejected by the CHECK
    with pytest.raises(asyncpg.CheckViolationError):
        async with db_conn.transaction():
            await db_conn.execute(
                "INSERT INTO tool_authority (tool_id, authority, source) "
                "VALUES ($1,'root_god_mode',$2::memory_source)",
                tool_id, MemorySource.INFERENCE.value)


async def test_only_one_current_authority_per_tool(db_conn: Any) -> None:
    # Authority is immutable-by-supersession: at most one open (valid_until IS NULL) row per tool.
    tool_id = await _insert_tool(db_conn)
    await db_conn.execute(
        "INSERT INTO tool_authority (tool_id, authority, source) VALUES ($1,'normal',$2::memory_source)",
        tool_id, MemorySource.INFERENCE.value)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with db_conn.transaction():
            await db_conn.execute(
                "INSERT INTO tool_authority (tool_id, authority, source) "
                "VALUES ($1,'elevated',$2::memory_source)",
                tool_id, MemorySource.SYSTEM_OBSERVATION.value)
    # closing the current row lets a new classification become current (history retained)
    await db_conn.execute(
        "UPDATE tool_authority SET valid_until=now() WHERE tool_id=$1 AND valid_until IS NULL", tool_id)
    await db_conn.execute(
        "INSERT INTO tool_authority (tool_id, authority, source) VALUES ($1,'elevated',$2::memory_source)",
        tool_id, MemorySource.SYSTEM_OBSERVATION.value)
    current = await db_conn.fetchval(
        "SELECT authority FROM tool_authority WHERE tool_id=$1 AND valid_until IS NULL", tool_id)
    total = await db_conn.fetchval("SELECT count(*) FROM tool_authority WHERE tool_id=$1", tool_id)
    assert current == "elevated" and total == 2  # supersede, never overwrite


async def test_authority_is_deleted_with_its_tool(db_conn: Any) -> None:
    tool_id = await _insert_tool(db_conn)
    await db_conn.execute(
        "INSERT INTO tool_authority (tool_id, authority, source) VALUES ($1,'normal',$2::memory_source)",
        tool_id, MemorySource.INFERENCE.value)
    await db_conn.execute("DELETE FROM discovered_tool WHERE id=$1", tool_id)
    orphans = await db_conn.fetchval("SELECT count(*) FROM tool_authority WHERE tool_id=$1", tool_id)
    assert orphans == 0  # ON DELETE CASCADE
