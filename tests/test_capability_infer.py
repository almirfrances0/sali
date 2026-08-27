"""Architecture review · Increment 11 — capability interpretation for unmapped binaries (§3/§8).

The model maps an unmapped tool to vocabulary slugs from its --help text, written as INFERENCE edges
below the curated rules (a real rule always wins). Each tool is attempted once; the vocabulary is
authoritative (the model can't invent a capability).
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.toolvocab import REL_PROVIDES_CAPABILITY
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.twin import tools
from sali.twin.capabilities import apply_capabilities, infer_unmapped, tools_with_capability
from sali.twin.interpret import interpret_capabilities

pytestmark = pytest.mark.db


async def test_interpret_capabilities_only_returns_vocabulary_slugs() -> None:
    # the model names a real slug + an invented one; only the real one survives
    provider = FakeModelProvider(responses=[ChatResult("json_processing, telepathy", None, [], 1, 1, "fake")])
    slugs = await interpret_capabilities(provider, "jaq", "jaq is a json processor")
    assert slugs == ["json_processing"]  # invented capability rejected


async def _probe_stub(_: str) -> str:
    return "a JSON processor for the command line"


async def test_infer_gives_an_unmapped_tool_an_inference_edge(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await tools.sync_tools(conn, {"jaq": "/usr/bin/jaq"}, {})  # discovered, no capability
        provider = FakeModelProvider(responses=[ChatResult("json_processing", None, [], 1, 1, "fake")])

        n = await infer_unmapped(conn, provider, limit=5, probe=_probe_stub)
        assert n == 1
        assert "jaq" in await tools_with_capability(conn, "json_processing")
        # the edge is INFERENCE-sourced (below curated EXTERNAL_SOURCE rules)
        src = await conn.fetchval(
            "SELECT e.source FROM graph_edge e JOIN discovered_tool t ON t.node_id=e.src_id "
            "WHERE t.name='jaq' AND e.rel_type=$1 AND e.valid_until IS NULL", REL_PROVIDES_CAPABILITY)
        assert src == "inference"


async def test_a_tool_is_attempted_only_once(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await tools.sync_tools(conn, {"mystery": "/usr/bin/mystery"}, {})

        async def _no_help(_: str) -> str:
            return ""  # no synopsis → no capability inferred

        provider = FakeModelProvider(responses=[ChatResult("none", None, [], 1, 1, "fake")])
        assert await infer_unmapped(conn, provider, limit=5, probe=_no_help) == 0
        # it's marked attempted, so a second pass skips it (never re-probes the ~2500-tool tail)
        assert await infer_unmapped(conn, provider, limit=5, probe=_no_help) == 0
        marked = await conn.fetchval(
            "SELECT structured->>'cap_attempted' FROM discovered_tool WHERE name='mystery'")
        assert marked == "true"


async def test_curated_rule_is_not_re_probed(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await tools.sync_tools(conn, {"jq": "/usr/bin/jq"}, {})
        await apply_capabilities(conn)  # jq gets json_processing from the curated EXTERNAL_SOURCE rule

        async def _boom(_: str) -> str:
            raise AssertionError("a curated tool must not be probed for capability inference")

        # jq already has a capability, so infer_unmapped skips it entirely (never calls the probe)
        assert await infer_unmapped(conn, FakeModelProvider(), limit=5, probe=_boom) == 0
