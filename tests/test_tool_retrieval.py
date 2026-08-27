"""Tool Intelligence — Increment 7: routing tool questions into retrieval (§7/§62).

A tool question is routed to a tool intent, surfaces the tools that provide the asked-for capability
(or an inventory summary), and lands as a droppable P2 section in the assembled context.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.context.engine import ContextEngine
from sali.provider.fake import FakeModelProvider
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.twin import tools
from sali.twin.capabilities import apply_capabilities, capability_slugs_in

pytestmark = pytest.mark.db


def test_router_detects_tool_intent() -> None:
    assert classify("which networking tools do I have?").intent == "tool"
    assert classify("is there a tool for cracking passwords?").use_tools is True
    assert classify("what tools are installed?").use_tools is True
    # a plain recall question is NOT tool intent
    assert classify("what did I say about the VPS?").use_tools is False


def test_capability_slugs_in_maps_language_to_slugs() -> None:
    assert "packet_capture" in capability_slugs_in("something to capture packets")
    assert "port_scanning" in capability_slugs_in("which networking tools do I have")
    assert "json_processing" in capability_slugs_in("a tool to process json")
    assert capability_slugs_in("what did I have for lunch") == []


async def _seed(conn: Any, *names: str) -> None:
    await tools.sync_tools(conn, {n: f"/usr/bin/{n}" for n in names}, {})
    await apply_capabilities(conn)


async def test_gather_surfaces_tools_for_a_capability(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await _seed(conn, "tcpdump", "jq", "nmap")

    svc = RetrievalService(live_pool, FakeModelProvider())
    bundle = await svc.gather("which tool captures packets?", classify("which tool captures packets?"))

    caps = {tf.capability: tf.tools for tf in bundle.tool_facts}
    assert "packet_capture" in caps and "tcpdump" in caps["packet_capture"]


async def test_gather_summarizes_inventory_when_no_capability_named(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await _seed(conn, "tcpdump", "jq")

    svc = RetrievalService(live_pool, FakeModelProvider())
    bundle = await svc.gather("what tools do I have installed?", classify("what tools do I have installed?"))

    assert bundle.tool_facts and bundle.tool_facts[0].capability == "inventory"
    assert "2 tools installed" in bundle.tool_facts[0].description


async def test_non_tool_query_gathers_no_tool_facts(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await _seed(conn, "tcpdump")
    svc = RetrievalService(live_pool, FakeModelProvider())
    bundle = await svc.gather("what did I say yesterday?", classify("what did I say yesterday?"))
    assert bundle.tool_facts == []  # gated — only tool intent pays for this retriever


def test_tool_facts_render_as_a_context_section() -> None:
    from sali.retrieval.models import RetrievalBundle, ToolFact

    engine = ContextEngine(FakeModelProvider(), ctx_tokens=4096)
    bundle = RetrievalBundle(tool_facts=[
        ToolFact(capability="packet_capture", description="Capture and inspect network traffic",
                 tools=["tcpdump", "tshark"])])
    result = engine.assemble("which tool captures packets?", bundle, [])
    system = result.messages[0].content
    assert "Tools on this machine" in system and "tcpdump" in system and "packet_capture" in system
    assert "tools_available" in result.included
