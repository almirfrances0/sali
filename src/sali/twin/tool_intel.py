"""The background Tool-Intelligence pass (spec §26/§27/§74).

One low-CPU, fully deterministic sweep — NO model — that keeps Sali's picture of its own tools
current: discover what's on PATH, map known tools to capabilities, and classify each tool's
execution authority. It is meant to ride the existing daemon's tick loop on a slow cadence, not to
run continuously (§28): the work is diff-based (a re-scan touches only what changed) so steady-state
cost is tiny.

Serialized by its OWN advisory lock (distinct from learning's) so a daemon pass and an ad-hoc run
never collide; a pass that can't get the lock skips cleanly. The subprocess scan (PATH + dpkg) runs
OUTSIDE the DB transaction — only the fold holds a transaction — so a slow dpkg never keeps a pooled
connection idle-in-transaction (the same discipline consolidation uses for its model calls).
Observation only: nothing here acts on the machine (§59/§60).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sali.core.enums import MemorySource
from sali.obs.log import get_logger
from sali.twin.authority import classify_tools
from sali.twin.capabilities import apply_capabilities
from sali.twin.tools import observe_path_tools, resolve_packages, sync_tools

log = get_logger("sali.twin.tool_intel")

_TOOL_INTEL_LOCK = 0x5A11_7001  # distinct from learning._CONSOLIDATE_LOCK (0x5A11C0DE)


@dataclass(slots=True)
class ToolIntelResult:
    discovered: int = 0
    added: int = 0
    removed: int = 0
    capability_edges: int = 0
    authority_written: int = 0
    skipped: bool = False   # another pass held the lock


async def run_pass(
    pool: Any, *, found: dict[str, str] | None = None, packages: dict[str, str] | None = None,
    source: MemorySource = MemorySource.SYSTEM_OBSERVATION,
) -> ToolIntelResult:
    """Run one full deterministic tool-intelligence sweep. `found`/`packages` may be injected (tests);
    otherwise the live machine is scanned. Owns its own connection + lock + transaction."""
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _TOOL_INTEL_LOCK):
            return ToolIntelResult(skipped=True)  # another pass is running — skip cleanly
        try:
            if found is None:  # scan the real machine OUTSIDE any transaction (subprocess work)
                found = observe_path_tools()
                packages = await resolve_packages(found)
            packages = packages or {}
            async with conn.transaction():
                disc = await sync_tools(conn, found, packages, source=source)
                caps = await apply_capabilities(conn)
                auth = await classify_tools(conn)
            await conn.execute(
                "INSERT INTO event (event_type, payload) VALUES ('tool.intel_pass', $1)",
                {"discovered": disc.total, "added": len(disc.added), "removed": len(disc.removed),
                 "capability_edges": caps.edges, "authority_written": auth.written})
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _TOOL_INTEL_LOCK)
    log.info("tool_intel_pass", discovered=disc.total, added=len(disc.added),
             removed=len(disc.removed), capability_edges=caps.edges, authority_written=auth.written)
    return ToolIntelResult(
        discovered=disc.total, added=len(disc.added), removed=len(disc.removed),
        capability_edges=caps.edges, authority_written=auth.written)
