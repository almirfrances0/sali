"""Memory evaluation harness (spec §67/§68).

Turns "I believe memory helps" into evidence: seeds a controlled, Almir-like scenario, runs the
DETERMINISTIC memory pipeline (retrieval, entity resolution, temporal queries, provenance,
contradiction handling, scope, corroboration), and scores each dimension against a known answer.
The model is never involved — this measures whether memory surfaces the RIGHT evidence, which is
what §67 asks for. Everything runs inside ONE rolled-back transaction, so it never touches real data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.graph.service import GraphService
from sali.memory.service import MemoryService
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _OneConn:
    """A pool that always hands back the SAME connection — so seeding and querying share one
    transaction that the harness rolls back, leaving the database untouched."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                return conn

            async def __aexit__(self, *a: Any) -> bool:
                return False

        return _Ctx()


@dataclass
class _Ctx:
    mem: MemoryService
    graph: GraphService
    retrieval: RetrievalService


@dataclass
class Case:
    name: str
    dimension: str
    run: Callable[[_Ctx], Awaitable[bool]]


# ── the benchmark cases (§67 dimensions + §33 example queries) ────────────────

async def _c_retrieval(ctx: _Ctx) -> bool:
    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC,
                           content="Project Zeta uses PostgreSQL for its primary datastore",
                           source=MemorySource.USER_EXPLICIT, importance=0.7)
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("what database does Project Zeta use", k=5)
    return any("PostgreSQL" in h.memory.content for h in hits)


async def _c_entity_self(ctx: _Ctx) -> bool:
    await ctx.graph.ensure_node(node_type="person", name="Almir", canonical_key="person:almir",
                                source=MemorySource.USER_EXPLICIT,
                                props={"aliases": ["me", "my", "i", "owner"]})
    nodes = await ctx.graph.find_by_name("me")
    return bool(nodes) and nodes[0].node_type == "person" and nodes[0].name == "Almir"


async def _c_entity_alias(ctx: _Ctx) -> bool:
    await ctx.graph.ensure_node(node_type="host", name="VPS-01", canonical_key="host:vps01",
                                source=MemorySource.USER_EXPLICIT,
                                props={"aliases": ["my vps", "the server", "production box"]})
    nodes = await ctx.graph.find_by_name("the server")
    return bool(nodes) and nodes[0].name == "VPS-01"


async def _c_temporal(ctx: _Ctx) -> bool:
    from sali.graph import writer as gw

    async with ctx.graph.pool.acquire() as c:
        oll = await gw.ensure_node(c, node_type="software", name="Ollama",
                                   canonical_key="software:oll", source=MemorySource.SYSTEM_OBSERVATION)
        v1 = await gw.ensure_node(c, node_type="version", name="0.30",
                                  canonical_key="ver:030", source=MemorySource.SYSTEM_OBSERVATION)
        v2 = await gw.ensure_node(c, node_type="version", name="0.40",
                                  canonical_key="ver:040", source=MemorySource.SYSTEM_OBSERVATION)
        await gw.set_fact(c, src_id=oll.id, rel_type="has_version", dst_id=v1.id,
                          source=MemorySource.SYSTEM_OBSERVATION, at=_T0)
        await gw.set_fact(c, src_id=oll.id, rel_type="has_version", dst_id=v2.id,
                          source=MemorySource.SYSTEM_OBSERVATION, at=_T0 + timedelta(days=7))
    then = [n["node"].name for n in await ctx.graph.neighbors(
        oll.id, rel_types=["has_version"], as_of=_T0 + timedelta(days=1))]
    now = [n["node"].name for n in await ctx.graph.neighbors(oll.id, rel_types=["has_version"])]
    return then == ["0.30"] and now == ["0.40"]  # last week's value vs today's


async def _c_provenance(ctx: _Ctx) -> bool:
    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC,
                           content="ripgrep is a fast grep alternative written in Rust",
                           source=MemorySource.EXTERNAL_SOURCE, needs_grounding=True, importance=0.5)
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("ripgrep grep alternative", k=5)
    hit = next((h for h in hits if "ripgrep" in h.memory.content), None)
    return hit is not None and hit.memory.source is MemorySource.EXTERNAL_SOURCE \
        and hit.memory.needs_grounding


async def _c_contradiction(ctx: _Ctx) -> bool:
    from sali.graph import writer as gw

    async with ctx.graph.pool.acquire() as c:
        gpu = await gw.ensure_node(c, node_type="hw", name="GPU", canonical_key="hw:gpu",
                                   source=MemorySource.SYSTEM_OBSERVATION)
        a = await gw.ensure_node(c, node_type="model", name="RTX-3060", canonical_key="m:3060",
                                 source=MemorySource.SYSTEM_OBSERVATION)
        b = await gw.ensure_node(c, node_type="model", name="RTX-4090", canonical_key="m:4090",
                                 source=MemorySource.SYSTEM_OBSERVATION)
        cc = await gw.ensure_node(c, node_type="model", name="RTX-3060-Ti", canonical_key="m:3060ti",
                                  source=MemorySource.SYSTEM_OBSERVATION)
        await gw.set_fact(c, src_id=gpu.id, rel_type="is", dst_id=a.id,
                          source=MemorySource.SYSTEM_OBSERVATION)
        await gw.set_fact(c, src_id=gpu.id, rel_type="is", dst_id=b.id,
                          source=MemorySource.USER_EXPLICIT)  # a claim conflicting an observable slot
        opened = await c.fetchval("SELECT count(*) FROM contradiction WHERE status='open'")
        await gw.set_fact(c, src_id=gpu.id, rel_type="is", dst_id=cc.id,
                          source=MemorySource.SYSTEM_OBSERVATION)  # re-observation verifies
        still_open = await c.fetchval("SELECT count(*) FROM contradiction WHERE status='open'")
        verified = await c.fetchval("SELECT count(*) FROM contradiction WHERE resolved_by='verification'")
    return bool(opened >= 1 and still_open == 0 and verified >= 1)


async def _c_scope(ctx: _Ctx) -> bool:
    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC, content="the widget config lives in the global default",
                           source=MemorySource.CONVERSATION, scope="global")
    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC, content="the widget config lives in demo/settings.toml",
                           source=MemorySource.CONVERSATION, scope="project:demo")
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("where does the widget config live", k=5, scope="project:demo")
    top = next((h for h in hits if "widget config" in h.memory.content), None)
    return top is not None and "demo/settings.toml" in top.memory.content  # in-project surfaced first


async def _c_graph_traversal(ctx: _Ctx) -> bool:
    async with ctx.graph.pool.acquire() as c:
        from sali.graph import writer as gw
        px = await gw.ensure_node(c, node_type="project", name="Project Q", canonical_key="proj:q",
                                  source=MemorySource.USER_EXPLICIT)
        vps = await gw.ensure_node(c, node_type="host", name="VPS-Q", canonical_key="host:q",
                                   source=MemorySource.USER_EXPLICIT)
        doc = await gw.ensure_node(c, node_type="service", name="Docker-Q", canonical_key="svc:q",
                                   source=MemorySource.USER_EXPLICIT)
        await gw.relate(c, src_id=px.id, dst_id=vps.id, rel_type="runs_on", source=MemorySource.USER_EXPLICIT)
        await gw.relate(c, src_id=vps.id, dst_id=doc.id, rel_type="runs", source=MemorySource.USER_EXPLICIT)
    q = "what is Project Q connected to?"
    bundle = await ctx.retrieval.gather(q, classify(q), k=8)
    return any(f.dst == "Docker-Q" and f.hops == 2 for f in bundle.graph_facts)  # reached two hops out


async def _c_multi_source(ctx: _Ctx) -> bool:
    from sali.graph import writer as gw

    async with ctx.graph.pool.acquire() as c:
        s = await gw.ensure_node(c, node_type="entity", name="ServiceR", canonical_key="e:sr",
                                 source=MemorySource.INFERENCE)
        d = await gw.ensure_node(c, node_type="entity", name="DepR", canonical_key="e:dr",
                                 source=MemorySource.INFERENCE)
        e1 = await gw.relate(c, src_id=s.id, dst_id=d.id, rel_type="depends_on",
                             source=MemorySource.INFERENCE, confidence=0.4)
        base = e1.confidence
        e2 = await gw.relate(c, src_id=s.id, dst_id=d.id, rel_type="depends_on",
                             source=MemorySource.SYSTEM_OBSERVATION, confidence=0.9)
    return e2.confidence > base  # independent corroboration raised confidence


CASES: list[Case] = [
    Case("recall a stored fact", "retrieval", _c_retrieval),
    Case("resolve self-reference to the person", "entity_resolution", _c_entity_self),
    Case("resolve an alias to its entity", "entity_resolution", _c_entity_alias),
    Case("answer as-of vs current", "temporal", _c_temporal),
    Case("carry a web fact's provenance", "provenance", _c_provenance),
    Case("open then verify a contradiction", "contradiction", _c_contradiction),
    Case("prefer the current project's memory", "scope", _c_scope),
    Case("reach a fact two hops out", "graph_traversal", _c_graph_traversal),
    Case("corroborate across sources", "provenance", _c_multi_source),
]


@dataclass
class Scorecard:
    by_dimension: dict[str, list[int]] = field(default_factory=dict)  # dim -> [passed, total]
    results: list[tuple[str, str, bool]] = field(default_factory=list)  # (name, dimension, passed)

    @property
    def passed(self) -> int:
        return sum(1 for *_, ok in self.results if ok)

    @property
    def total(self) -> int:
        return len(self.results)


async def run_benchmark(pool: Any, provider: Any) -> Scorecard:
    """Run the whole benchmark in one rolled-back transaction (never persists). Returns a scorecard."""
    card = Scorecard()
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            one = _OneConn(conn)
            ctx = _Ctx(mem=MemoryService(one, provider), graph=GraphService(one),
                       retrieval=RetrievalService(one, provider))
            for case in CASES:
                try:
                    ok = await case.run(ctx)
                except Exception:  # noqa: BLE001 - a case that errors is a FAIL, never a crash
                    ok = False
                card.results.append((case.name, case.dimension, ok))
                slot = card.by_dimension.setdefault(case.dimension, [0, 0])
                slot[0] += int(ok)
                slot[1] += 1
        finally:
            await tx.rollback()  # the benchmark data never touches the real store
    return card
