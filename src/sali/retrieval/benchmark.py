"""Memory evaluation harness (spec §67/§68).

Turns "I believe memory helps" into evidence: seeds a controlled, Almir-like scenario, runs the
DETERMINISTIC memory pipeline (retrieval, entity resolution, temporal queries, provenance,
contradiction handling, scope, corroboration), and scores each dimension against a known answer.
The model is never involved — this measures whether memory surfaces the RIGHT evidence, which is
what §67 asks for. Everything runs inside ONE rolled-back transaction, so it never touches real data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
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
    """A pool that always hands back the SAME connection - so seeding and querying share one
    transaction that the harness rolls back, leaving the database untouched.

    The lock is what lets the parallelised `RetrievalService.gather` work here: a real pool hands out
    a distinct connection per task and asyncpg is happy, but a single connection cannot serve two
    coroutines at once ("cannot perform operation: another operation is in progress"). Serialising
    acquire() gives the benchmark the same total work in the same order, without the harness having
    to fake being a real pool."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    def acquire(self) -> Any:
        lock = self._lock
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                await lock.acquire()
                return conn

            async def __aexit__(self, *a: Any) -> bool:
                lock.release()
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


# ── A–Q end-to-end coverage (§11): each verifies the RETRIEVED FACTS, never an LLM answer ────────

async def _c_identity(ctx: _Ctx) -> bool:  # A. Identity resolves deterministically to the agent
    await ctx.graph.ensure_node(node_type="agent", name="Sali", canonical_key="agent:sali",
                                source=MemorySource.USER_EXPLICIT)
    await ctx.graph.ensure_node(node_type="project", name="sali", canonical_key="project:sali",
                                source=MemorySource.SYSTEM_OBSERVATION)
    res = await ctx.graph.resolve("Sali")
    return res.resolved is not None and res.resolved.canonical_key == "agent:sali" and res.ambiguous


async def _c_user(ctx: _Ctx) -> bool:  # B. User knowledge
    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC, content="Almir's primary OS is Kali Linux",
                           source=MemorySource.USER_EXPLICIT, importance=0.7)
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("what OS does Almir use", k=5)
    return any("Kali" in h.memory.content for h in hits)


async def _c_environment(ctx: _Ctx) -> bool:  # C. Environment
    await ctx.mem.remember(layer=MemoryLayer.SYSTEM_ENV, content="the machine has an NVIDIA RTX 4070 GPU",
                           source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key="env:gpu")
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("what GPU does the machine have", k=5)
    return any("RTX 4070" in h.memory.content for h in hits)


async def _c_episodic(ctx: _Ctx) -> bool:  # E. Episodic retrieval
    await ctx.mem.remember(layer=MemoryLayer.EPISODIC,
                           content="yesterday we fixed the Docker networking problem",
                           source=MemorySource.INFERENCE)
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("docker networking problem", k=5, layer="episodic")
    return any("Docker networking" in h.memory.content for h in hits)


async def _c_procedural(ctx: _Ctx) -> bool:  # F. Procedural retrieval surfaces on a task turn
    await ctx.mem.remember(layer=MemoryLayer.PROCEDURAL,
                           content="deploy service X: build then ship then verify",
                           source=MemorySource.INFERENCE, functional=True, claim_key="procedure:deployx",
                           structured={"steps": ["build", "ship", "verify"], "certainty": "learned"})
    await ctx.mem.embed_pending()
    q = "how do I deploy service X"
    bundle = await ctx.retrieval.gather(q, classify(q), k=5)
    return any("deploy service X" in h.memory.content for h in bundle.procedures)


async def _c_confidence(ctx: _Ctx) -> bool:  # J. Confidence — a weak belief is not a fact
    from sali.core.knowledge import KnowledgeType, classify_knowledge

    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC, content="the cache is probably Redis",
                           source=MemorySource.INFERENCE)  # gated → needs_grounding
    await ctx.mem.embed_pending()
    hits = await ctx.mem.retrieve("what is the cache", k=5)
    hit = next((h for h in hits if "cache" in h.memory.content), None)
    if hit is None:
        return False
    kt = classify_knowledge(hit.memory.source, hit.memory.layer,
                            needs_grounding=hit.memory.needs_grounding, confidence=hit.effective_confidence)
    return kt in (KnowledgeType.BELIEF, KnowledgeType.INFERENCE)  # honestly weak, not asserted as fact


async def _c_cross_session(ctx: _Ctx) -> bool:  # L. Cross-session persistence
    await ctx.mem.remember(layer=MemoryLayer.SEMANTIC, content="the deploy key lives in the vault",
                           source=MemorySource.CONVERSATION)
    await ctx.mem.embed_pending()
    later = MemoryService(ctx.graph.pool, ctx.mem.provider)  # a fresh service (a "later session"), same store
    hits = await later.retrieve("where is the deploy key", k=5)
    return any("deploy key" in h.memory.content for h in hits)


async def _c_reboot(ctx: _Ctx) -> bool:  # M. Reboot persistence — durably in the store
    m = await ctx.mem.remember(layer=MemoryLayer.IDENTITY, content="I am Sali, Almir's local AI",
                               source=MemorySource.USER_EXPLICIT)
    async with ctx.graph.pool.acquire() as c:
        row = await c.fetchrow("SELECT content FROM memory WHERE id=$1 AND valid_until IS NULL", m.id)
    return row is not None and "Sali" in row["content"]


async def _c_failure_learning(ctx: _Ctx) -> bool:  # N. Failure learning surfaces on a similar task
    await ctx.mem.remember(
        layer=MemoryLayer.EPISODIC,
        content="deploying failed because port 3000 was busy; fixed by freeing the port",
        source=MemorySource.INFERENCE, functional=True, claim_key="failure:x",
        structured={"kind": "incident", "error": "port 3000 busy", "correction": "free the port"})
    await ctx.mem.embed_pending()
    q = "deploying is failing again"
    bundle = await ctx.retrieval.gather(q, classify(q), k=5)
    return any("port 3000" in h.memory.content for h in bundle.experiences)


async def _c_procedure_learning(ctx: _Ctx) -> bool:  # O. Procedure learning from repeated runs
    from uuid import uuid4

    from sali.learning.procedures import learn_procedures

    async with ctx.graph.pool.acquire() as c:
        for _ in range(2):  # the same sequence in two runs clears the evidence bar
            run = uuid4()
            for cmd in ("docker compose build", "docker compose up -d"):
                await c.execute(
                    "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
                    "VALUES ($1,'execute_command',$2,true,'observed')", run, {"args": {"command": cmd}})
        learned = await learn_procedures(c, ctx.mem.provider, threshold=2)
    return len(learned) >= 1


async def _c_consolidation(ctx: _Ctx) -> bool:  # P. Consolidation mines a tool experience
    from uuid import uuid4

    from sali.learning.tool_experience import learn_tool_experiences

    async with ctx.graph.pool.acquire() as c:
        for _ in range(3):
            await c.execute(
                "INSERT INTO tool_execution (run_id, tool_name, plan, success, status) "
                "VALUES ($1,'execute_command',$2,true,'observed')", uuid4(),
                {"args": {"command": "jq . data.json"}})
        exps = await learn_tool_experiences(c, threshold=3)
    return any(e.binary == "jq" for e in exps)


async def _c_gap_detection(ctx: _Ctx) -> bool:  # Q. Knowledge-gap detection
    from sali.learning.queue import Budget, pending, queue_gaps

    # A distinctive subject that sorts FIRST, so the harness is robust even when the live store already
    # holds real investigate events (the benchmark shares the connection in a rolled-back transaction).
    subject = "AAA-benchmark-investigate-gap"
    async with ctx.graph.pool.acquire() as c:
        await c.execute(
            "INSERT INTO event (event_type, subject_type, payload) VALUES ('desktop.observed','desktop',$1)",
            {"kind": "port_opened", "summary": subject, "action": "investigate"})
        await queue_gaps(c, budget=Budget(max_new_per_pass=5))
        items = await pending(c, limit=50)
    return any(subject in i.subject for i in items)


CASES: list[Case] = [
    Case("identity resolves to the agent", "A_identity", _c_identity),
    Case("recall a fact about Almir", "B_user", _c_user),
    Case("recall an environment fact", "C_environment", _c_environment),
    Case("recall a stored fact", "D_retrieval", _c_retrieval),
    Case("recall an episode", "E_episodic", _c_episodic),
    Case("surface a procedure on a task turn", "F_procedural", _c_procedural),
    Case("reach a fact two hops out", "G_graph", _c_graph_traversal),
    Case("answer as-of vs current", "H_temporal", _c_temporal),
    Case("open then verify a contradiction", "I_contradiction", _c_contradiction),
    Case("keep a weak belief honestly weak", "J_confidence", _c_confidence),
    Case("carry a web fact's provenance", "K_provenance", _c_provenance),
    Case("corroborate across sources", "K_provenance", _c_multi_source),
    Case("persist across sessions", "L_cross_session", _c_cross_session),
    Case("persist durably in the store", "M_reboot", _c_reboot),
    Case("recall a past failure on a retry", "N_failure_learning", _c_failure_learning),
    Case("learn a procedure from repeats", "O_procedure_learning", _c_procedure_learning),
    Case("mine a tool experience", "P_consolidation", _c_consolidation),
    Case("detect a knowledge gap", "Q_gap_detection", _c_gap_detection),
    Case("resolve self-reference to the person", "entity_resolution", _c_entity_self),
    Case("resolve an alias to its entity", "entity_resolution", _c_entity_alias),
    Case("prefer the current project's memory", "scope", _c_scope),
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
