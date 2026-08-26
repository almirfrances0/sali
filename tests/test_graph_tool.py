"""The relate tool (§7-8): a stated relationship becomes a graph edge with honest provenance."""

from __future__ import annotations

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemorySource
from sali.tools.builtins.graph_tool import Relate
from sali.tools.context import ToolContext


class _FakeGraph:
    def __init__(self) -> None:
        self.links: list[tuple[str, str, str, MemorySource, float]] = []

    async def link(self, *, subject: str, relation: str, obj: str, source: MemorySource,
                   confidence: float = 0.6) -> None:
        self.links.append((subject, relation, obj, source, confidence))


def _ctx(graph: _FakeGraph | None) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), graph=graph)


async def test_relate_writes_edge_with_conversation_provenance() -> None:
    g = _FakeGraph()
    res = await Relate().run(
        {"subject": "Salix Studio", "relation": "deployed on", "object": "VPS-01"}, _ctx(g))
    assert res.ok
    subj, rel, obj, source, _conf = g.links[0]
    assert (subj, rel, obj) == ("Salix Studio", "deployed on", "VPS-01")
    assert source is MemorySource.CONVERSATION  # a model-asserted relationship isn't ground truth


async def test_relate_guards_missing_fields_and_graph() -> None:
    incomplete = await Relate().run({"subject": "x", "relation": "y"}, _ctx(_FakeGraph()))
    assert not incomplete.ok and "object" in (incomplete.error or "").lower()

    no_graph = await Relate().run({"subject": "a", "relation": "b", "object": "c"}, _ctx(None))
    assert not no_graph.ok and "graph" in (no_graph.error or "").lower()
