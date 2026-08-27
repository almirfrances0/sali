"""Architecture review · conflict cleanup — unify software:<x> and ext_tool:<x> for the same binary.

The twin's software node (version) and tool-intelligence's ext_tool node (capabilities/authority) for
the same program are linked with a same_program edge, so a query from either reaches the whole truth.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemorySource
from sali.core.toolvocab import ext_tool_key
from sali.graph.writer import ensure_node
from sali.twin import tools

pytestmark = pytest.mark.db


async def test_same_program_links_software_and_ext_tool(db_conn: Any) -> None:
    # the twin's software node for git (carries version + resolved path)
    await ensure_node(db_conn, node_type="software", name="Git 2.4", canonical_key="software:git",
                      source=MemorySource.SYSTEM_OBSERVATION,
                      props={"version": "2.4", "path": "/usr/bin/git"})
    # tool-intelligence's ext_tool node for the same binary
    await tools.sync_tools(db_conn, {"git": "/usr/bin/git"}, {})

    linked = await tools.link_tool_software(db_conn)
    assert linked == 1

    edge = await db_conn.fetchrow(
        "SELECT dst.canonical_key FROM graph_edge e "
        "  JOIN graph_node src ON src.id=e.src_id JOIN graph_node dst ON dst.id=e.dst_id "
        "WHERE src.canonical_key=$1 AND e.rel_type='same_program' AND e.valid_until IS NULL",
        ext_tool_key("git"))
    assert edge is not None and edge["canonical_key"] == "software:git"

    # idempotent — re-linking doesn't duplicate
    await tools.link_tool_software(db_conn)
    n = await db_conn.fetchval(
        "SELECT count(*) FROM graph_edge e JOIN graph_node src ON src.id=e.src_id "
        "WHERE src.canonical_key=$1 AND e.rel_type='same_program' AND e.valid_until IS NULL",
        ext_tool_key("git"))
    assert n == 1


async def test_no_link_when_ext_tool_absent(db_conn: Any) -> None:
    await ensure_node(db_conn, node_type="software", name="Rust", canonical_key="software:rustc",
                      source=MemorySource.SYSTEM_OBSERVATION, props={"path": "/usr/bin/rustc"})
    # no ext_tool:rustc discovered → nothing to link
    assert await tools.link_tool_software(db_conn) == 0
