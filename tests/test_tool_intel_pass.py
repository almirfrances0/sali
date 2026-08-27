"""Tool Intelligence — Increment 6: the background tool-intelligence pass (§26/§27/§74).

One deterministic sweep — discover → map capabilities → classify authority — serialized by its own
advisory lock, idempotent, observation-only. This is what the daemon runs on a slow cadence.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.twin import tool_intel
from sali.twin.authority import current_authority
from sali.twin.capabilities import tools_with_capability

pytestmark = pytest.mark.db

_FOUND = {"nmap": "/usr/bin/nmap", "jq": "/usr/bin/jq", "mkfs": "/usr/sbin/mkfs"}


async def test_pass_discovers_maps_and_classifies_in_one_sweep(live_pool: Any) -> None:
    from sali.core.enums import ExecAuthority

    res = await tool_intel.run_pass(live_pool, found=_FOUND, packages={"nmap": "nmap"})

    assert not res.skipped and res.discovered == 3 and res.added == 3
    assert res.capability_edges > 0 and res.authority_written == 3
    async with live_pool.acquire() as conn:
        assert "nmap" in await tools_with_capability(conn, "port_scanning")   # capability mapped
        assert await current_authority(conn, "mkfs") is ExecAuthority.SYSTEM_CRITICAL  # classified
        assert await current_authority(conn, "jq") is ExecAuthority.NORMAL


async def test_pass_is_idempotent(live_pool: Any) -> None:
    await tool_intel.run_pass(live_pool, found=_FOUND, packages={})
    again = await tool_intel.run_pass(live_pool, found=_FOUND, packages={})
    assert again.added == 0 and again.removed == 0 and again.authority_written == 0


async def test_pass_skips_cleanly_when_another_holds_the_lock(live_pool: Any) -> None:
    async with live_pool.acquire() as holder:
        await holder.fetchval("SELECT pg_advisory_lock($1)", tool_intel._TOOL_INTEL_LOCK)
        try:
            res = await tool_intel.run_pass(live_pool, found=_FOUND, packages={})
            assert res.skipped is True  # never blocks, never double-runs
        finally:
            await holder.execute("SELECT pg_advisory_unlock($1)", tool_intel._TOOL_INTEL_LOCK)
