"""The agent execution loop — the explicit FSM at Sali's core.

INPUT → RETRIEVE → BUILD_CONTEXT → REASON_PLAN → [AWAIT_CONFIRM → EXECUTE_TOOL → OBSERVE →
VERIFY → UPDATE_STATE]* → LEARN → RESPOND. The LLM is called at exactly one hot-loop site
(REASON_PLAN); retrieve, dispatch, verify, and journaling are deterministic. Every tool
effect is journaled ``executing`` *before* it runs (fix M15) and verified after — a tool is
never assumed to have succeeded (rule 13).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sali.browser.service import build_browser
from sali.comms.service import CommsService
from sali.config.secrets import SecretStore
from sali.config.settings import Settings
from sali.context.engine import LIVE_NOTE, ContextEngine
from sali.core.clock import Clock, SystemClock
from sali.core.enums import MemoryLayer, MemorySource, RiskLevel
from sali.core.errors import ProviderError
from sali.core.ids import new_id
from sali.core.knowledge import classify_knowledge, epistemic_status
from sali.core.scope import project_scope
from sali.core.toolvocab import binary_of
from sali.graph.service import GraphService
from sali.ingest.service import IngestService
from sali.learning.episodes import prune_stm
from sali.memory.writer import observe
from sali.obs.log import get_logger
from sali.perception.service import build_perception
from sali.provider.base import ChatMessage, ChatResult, ModelProvider, ToolCall
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.runtime import context_budget, continuation, pending
from sali.runtime.health import HealthService
from sali.runtime.journal import RunJournal
from sali.runtime.referential import extract_proposed_command, is_delegation
from sali.runtime.self_state import SelfStateStore
from sali.runtime.state import ResumeAction, RunState, resume_action
from sali.runtime.world_state import WorldStateBuilder
from sali.scheduler.store import ScheduleStore
from sali.security.confirm import Confirmer
from sali.security.policy import Action, PolicyDecision, PolicyEngine
from sali.security.redact import redact_obj
from sali.tasks.authority import TaskAuthority
from sali.tasks.logger import append_event
from sali.tasks.store import TaskStore
from sali.tools import dispatch
from sali.tools.base import VerifyResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry
from sali.tools.remote import build_remote_runner
from sali.twin import awareness as twin_awareness
from sali.verify.engine import verify_effect

# Tools whose arguments carry a raw shell command — the binary they run is what the tool-authority
# floor is keyed on.
_COMMAND_TOOLS = frozenset({"execute_command", "ssh_run"})
_AUTHORITY_TTL = 300.0  # seconds — refresh the system-critical binary set at most this often


def _command_binary(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name not in _COMMAND_TOOLS:
        return None
    cmd = args.get("command")
    return binary_of(cmd) if isinstance(cmd, str) and cmd else None


def _retrieval_trace(bundle: Any) -> dict[str, Any]:
    """Why each memory was selected (§7): candidate count, which retriever found it, and the ranking
    signals (score/similarity/confidence/freshness) + epistemic kind of the top hits. Journaled for
    diagnostics; never shown to Almir by default."""
    hits = bundle.memories
    by_retriever: dict[str, int] = {}
    for h in hits:
        by_retriever[h.retriever] = by_retriever.get(h.retriever, 0) + 1
    top: list[dict[str, Any]] = []
    for h in hits[:5]:
        m = h.memory
        ktype = classify_knowledge(m.source, m.layer, needs_grounding=m.needs_grounding,
                                   confidence=h.effective_confidence)
        top.append({
            "content": m.content[:60], "retriever": h.retriever, "score": round(h.score, 3),
            "similarity": round(h.similarity, 3) if h.similarity is not None else None,
            "confidence": round(h.effective_confidence, 2), "freshness": round(h.freshness_factor, 2),
            "stale": h.stale, "knowledge_type": ktype.value,
        })
    return {
        "candidates": len(hits), "by_retriever": by_retriever,
        "graph_contribution": len(bundle.graph_facts),
        "experience_contribution": len(bundle.procedures) + len(bundle.experiences),
        "top": top,
    }

# Warmer sampling so Sali sounds like a person, not a deterministic tool. Tool-calling is
# rendered structurally by the model, so it still works reliably at this temperature.
_VOICE: dict[str, Any] = {
    "temperature": 0.7, "top_k": 40, "top_p": 0.95, "presence_penalty": 0.3, "repeat_penalty": 1.1,
}
_SUMMARIZE: dict[str, Any] = {"temperature": 0.3, "top_k": 40, "top_p": 0.9}

# Fold older turns into a running summary once this many uncompacted messages accumulate,
# always keeping the most recent few verbatim. Generous — let Sali keep more context alive.
_COMPACT_AFTER = 40
_KEEP_RECENT = 10

# Intra-turn context folding: when the *working* prompt for a single task nears the model's window,
# fold everything done so far into a compact progress note and carry on — so a long, many-step job
# continues on its own instead of dead-ending on the context limit. The budget/thresholds now live in
# runtime.context_budget (provider-derived window, output reservation, safe→emergency status ladder).
# A context overflow is recovered a bounded number of times per turn: fold hard, then retry (§8/§29).
_MAX_OVERFLOW_RECOVERIES = 3
# Follow-through: models sometimes *narrate* an action ("I'll run these in parallel") and then
# stop without calling anything, leaving Almir to re-prompt. When a reply defers a machine
# action but emits no tool call, we nudge Sali to actually do it — bounded, so it can't loop.
# The model occasionally emits a malformed tool call the backend can't parse (a transient 500).
# Re-sampling at the warm voice temperature almost always yields a clean one, so retry a couple
# of times before giving up rather than crashing the whole turn.
_MAX_PROVIDER_RETRIES = 3

# Enough pushes to carry a large multi-step build (many files, tests, deploy) to completion,
# bounded so it can never spin. With 65K ctx, Sali can hold more steps in mind at once.
_MAX_FOLLOW_THROUGH = 8
_FOLLOW_THROUGH_NUDGE = (
    "(You haven't finished, and you have no background process — nothing runs on its own. Do ALL "
    "the remaining steps NOW, in this reply: make every tool call needed to finish the whole task "
    "end to end. Don't stop between steps or say you'll 'continue' — just finish it, then tell me "
    "it's done.)"
)

# Sali sometimes gets stuck investigating — re-reading the same files, re-listing the same dirs —
# instead of acting. When it repeats tool calls it already made, that's a loop; break it.
_LOOP_REPEATS = 3
_ANTI_LOOP_NUDGE = (
    "(You're repeating tool calls you've already made — you're going in circles. Stop investigating: "
    "make the change or fix directly NOW, or if you're genuinely stuck, stop and tell Almir plainly "
    "what you found and what's blocking you. Don't read or list anything you've already looked at.)"
)
# When the model ends a turn with NOTHING to say (e.g. it gave up after failing tool attempts), never
# leave Almir staring at a blank reply — make it say, in its own voice, what happened or what blocked it.
_EMPTY_WRAP_NUDGE = (
    "(You just returned an empty reply — Almir would see nothing. Tell him plainly, in your own words, "
    "what you found, what you did, or exactly what blocked you and why. Don't call any tools.)"
)
_EMPTY_FALLBACK = (
    "I couldn't finish that — I hit a wall and don't have a clear result to give you. "
    "Want me to try a different way?"
)
# The SAME (tool, args) call failing identically this many times → stop dispatching it and hand the
# failure back as a factual result, so a wrong call (e.g. a hallucinated path) can't be retried forever.
_MAX_TOOL_FAILURES = 3
# How often the background consolidation pass (§17-19: mine procedures, record failures, distill an
# episode) may run. Throttled so a busy session doesn't distill every turn; it runs OFF-THREAD after
# the turn is already done, so it never adds latency to Almir's reply.
_CONSOLIDATE_EVERY_S = 300.0

# A tool call sometimes leaks out as *text* instead of a parsed call (the model emits the JSON
# itself). We must never leave Almir staring at raw JSON, so we detect it and ask for a clean redo.
_MAX_JSON_RECOVERY = 2
_JSON_RECOVERY_NUDGE = (
    "(That came out as raw JSON instead of doing anything. If you meant to use a tool, call it "
    "properly as a tool. Otherwise just answer me in plain words — never paste JSON at me.)"
)
_LEAKED_TOOL_JSON = re.compile(
    r'^\s*[\[{].*"(name|tool_name|function|arguments|parameters)"\s*:', re.IGNORECASE | re.DOTALL
)


def _too_similar(current: str, previous: str) -> bool:
    """Is this reply essentially a repeat of the last one? Used to stop Sali saying the same thing
    over and over when it's stuck (e.g. a tool keeps failing and it keeps narrating 'let me retry')."""
    if not current or not previous:
        return False
    a, b = " ".join(current.lower().split()), " ".join(previous.lower().split())
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() > 0.85


def _looks_like_leaked_tool_call(text: str) -> bool:
    """A response whose content is really a tool-call JSON blob the parser didn't catch. We treat
    it as a malfunction to recover from, not as an answer to show."""
    stripped = text.strip()
    return stripped.startswith(("{", "[")) and bool(_LEAKED_TOOL_JSON.match(stripped))


class _TokenGate:
    """Streams Sali's words live, but holds back a reply that is really a leaked tool-call JSON
    blob so the raw JSON is never shown (the loop then has the model redo it cleanly). It keys on
    the same signal as `_looks_like_leaked_tool_call`: real prose never opens with '{' or '[', a
    leaked blob always does. So the first non-whitespace character decides — prose is released and
    then flows live; a blob is withheld for the whole turn. Structural, not a prompt rule."""

    __slots__ = ("_buf", "_streaming")

    def __init__(self) -> None:
        self._buf = ""
        self._streaming: bool | None = None  # None = undecided, True = prose, False = withholding

    def feed(self, chunk: str) -> str:
        """Return the text safe to show now — '' while still undecided or withholding a blob."""
        if self._streaming is True:
            return chunk
        self._buf += chunk
        if self._streaming is False:
            return ""
        head = self._buf.lstrip()
        if not head:
            return ""  # only whitespace so far — can't tell yet
        if head[0] in "{[":
            self._streaming = False  # opens like a tool-call blob — hold it back for recovery
            return ""
        self._streaming = True  # it's prose — release what's buffered and stream live from here
        out, self._buf = self._buf, ""
        return out


# Stall detection is structural, not a phrase list, and has NO length cutoff: a stall can hide at
# the end of a long explanation ("…let me now write all the files"), so every tool-less reply is
# put to the model, which judges its OWN reply — did it finish, or only announce/half-do it? This
# is what the model is for (judgement, not parsing), and it can't drift out of date like a verb list.
_STALL_JUDGE_SYSTEM = (
    "Almir asked you to do something and you just replied. Judge your OWN reply against the FULL "
    "task: did you actually finish everything he asked and give him the result — or did you only "
    "announce it, do PART of it, or say you'll 'continue' / 'keep building' / 'work on it' WITHOUT "
    "actually finishing in this reply? You have no background process, so if there's more to do, it "
    "is NOT done. A genuine question back to Almir, or a fully-finished task, counts as DONE. Reply "
    "with exactly one word: DONE or STALLED."
)


class _MemorySink:
    """Bridges the remember tool to the memory system: write a durable fact, then embed it so it's
    immediately recallable. Injected into ToolContext so the tools layer needn't import memory."""

    def __init__(self, service: Any) -> None:  # MemoryService
        self._service = service

    async def remember(
        self, content: str, *, source: MemorySource, note: str | None = None,
        importance: float = 0.6, needs_grounding: bool = False, about: str | None = None,
    ) -> None:
        # An `about` topic makes this a functional claim: restating a fact about the same topic
        # supersedes the old value (evidence-priority) instead of piling up a contradiction.
        claim_key = f"remember:{about.strip().lower()}" if about and about.strip() else None
        await self._service.remember(
            layer=MemoryLayer.SEMANTIC, content=content, source=source,
            importance=importance, note=note, needs_grounding=needs_grounding,
            functional=bool(claim_key), claim_key=claim_key,
        )
        await self._service.embed_pending()

    async def forget(self, query: str, *, reason: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._service.forget_matching(query, reason=reason)
        return result

    async def verify(self, query: str, *, verified: bool, note: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = await self._service.verify_matching(query, verified=verified, note=note)
        return result


class _SelfSink:
    """Exposes Sali's runtime self-model to the self_state tool (§6/§7/§41/§71). Read-only."""

    def __init__(self, store: SelfStateStore) -> None:
        self._store = store

    async def report(self) -> dict[str, Any]:
        return await self._store.assemble()


class _HealthSink:
    """Exposes Sali's live subsystem/internet health to the system_health tool (§51/§52/§53)."""

    def __init__(self, service: HealthService) -> None:
        self._service = service

    async def report(self) -> dict[str, Any]:
        h = await self._service.check()
        return {"subsystems": h.subsystems, "degraded": h.degraded, "online": h.internet,
                "summary": h.render(), "detail": h.detail}


class _ToolCatalogSink:
    """Lets Sali query its OWN toolset (§53/§21): which installed tools serve a task (ranked by learned
    reliability + safety) and a tool's alternatives — over the inventory + capability graph. Read-only."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def suggest(self, task: str) -> list[dict[str, Any]]:
        from sali.twin import selection

        async with self._pool.acquire() as conn:
            ranked = await selection.suggest(conn, task)
        return [{"tool": s.name, "capability": s.capability, "score": s.score,
                 "reliability": s.reliability, "used": s.used, "authority": s.authority}
                for s in ranked]

    async def alternatives(self, tool: str) -> list[str]:
        from sali.twin import selection

        async with self._pool.acquire() as conn:
            return await selection.alternatives(conn, tool)


class _RecallSink:
    """Active memory recall for Sali's memory tools (§34,§55,§56): hybrid search, graph traversal, and
    history — every result carries provenance + confidence + a staleness flag so the model reasons
    over evidence and never has to invent a memory (§47). Read-only, over the retrieval + graph engines."""

    def __init__(self, memory: Any, graph: Any) -> None:  # MemoryService, GraphService
        self._memory = memory
        self._graph = graph

    async def search(self, query: str, *, layer: str | None = None, k: int = 6) -> list[dict[str, Any]]:
        # A layer-scoped read is a REAL SQL predicate now, so a relevant procedure/incident outside
        # the generic top-k is no longer invisible (fixes the top-24 blind spot; §4/§6).
        hits = await self._memory.retrieve(query, k=k, layer=layer)
        out: list[dict[str, Any]] = []
        for h in hits[:k]:
            m = h.memory
            # The epistemic kind — did Sali OBSERVE this, INFER it, or merely BELIEVE it? — so the model
            # weights each recalled item by how it's actually known, not by how fluent it sounds (§3/§12).
            ktype = classify_knowledge(m.source, m.layer, needs_grounding=m.needs_grounding,
                                       confidence=h.effective_confidence)
            entry: dict[str, Any] = {
                "content": m.content, "layer": m.layer.value, "source": m.source.value,
                "knowledge_type": ktype.value,
                "epistemic_status": epistemic_status(ktype, h.effective_confidence),
                "confidence": round(h.effective_confidence, 2), "stale": h.stale,
                "recorded": m.valid_from.date().isoformat() if m.valid_from else None,
            }
            if m.structured:  # a structured incident/experience/procedure — give Sali the whole shape
                entry["detail"] = m.structured
            out.append(entry)
        return out

    async def related(self, entity: str, *, hops: int = 1) -> dict[str, Any]:
        # Resolve EXPLAINABLY (§5/§6): which entity was chosen, how, and the others the name matched —
        # so two queries can never silently disagree about which 'Sali' they meant.
        res = await self._graph.resolve(entity)
        if res.resolved is None:
            return {"entity": entity, "found": False, "relations": [], "candidates": []}
        node = res.resolved
        relations: list[dict[str, Any]] = []
        if hops <= 1:
            for hop in await self._graph.neighbors(node.id):
                relations.append({"relation": hop["rel_type"], "target": hop["node"].name,
                                  "type": hop["node"].node_type, "confidence": round(hop["confidence"], 2)})
        else:
            for r in await self._graph.traverse(node.id, max_depth=min(hops, 4)):
                if r["depth"] > 0:
                    relations.append({"target": r["name"], "type": r["node_type"], "hops": r["depth"]})
        others = [c for c in res.candidates if c["canonical_key"] != node.canonical_key][:5]
        return {
            "entity": entity, "found": True,
            "resolved": {"name": node.name, "canonical_key": node.canonical_key,
                         "node_type": node.node_type, "matched_by": res.matched_by},
            "ambiguous": res.ambiguous, "other_candidates": others,
            "scope": "current graph (temporal-filtered, valid_until IS NULL)",
            "relations": relations[:24],
        }

    async def entity(self, name: str) -> dict[str, Any]:
        nodes = await self._graph.find_by_name(name, limit=3)
        if not nodes:
            return {"name": name, "found": False}
        node = nodes[0]
        neighbours = await self._graph.neighbors(node.id)
        return {"name": node.name, "found": True, "type": node.node_type,
                "confidence": round(node.confidence, 2), "props": redact_obj(dict(node.props)),
                "relations": [{"relation": h["rel_type"], "target": h["node"].name} for h in neighbours[:24]]}

    async def history(self, entity: str, relation: str) -> dict[str, Any]:
        nodes = await self._graph.find_by_name(entity, limit=1)
        if not nodes:
            return {"entity": entity, "found": False, "timeline": []}
        node = nodes[0]
        rel = relation.strip().lower().replace(" ", "_")
        edges = await self._graph.history(node.id, rel)
        names = await self._graph.names_for([e.dst_id for e in edges])
        timeline = [{
            "value": names.get(e.dst_id, str(e.dst_id)),
            "from": e.valid_from.date().isoformat() if e.valid_from else None,
            "until": e.valid_until.date().isoformat() if e.valid_until else None,
            "current": e.valid_until is None,
        } for e in edges]
        return {"entity": node.name, "relation": rel, "found": bool(timeline), "timeline": timeline}


class _VisionSink:
    """Bridges the see_screen tool to the LOCAL vision model. The screenshot bytes go only to the
    provider (local Ollama) and are never stored — the tools layer never imports the provider."""

    def __init__(self, provider: Any) -> None:  # ModelProvider
        self._provider = provider

    async def look(self, prompt: str, image: bytes) -> str:
        return str(await self._provider.describe_image(prompt, image))


class _ResearchSink:
    """Just-in-time web research bound to the current task (Prompt 5 §14-20). Searches the web, distills
    a bounded extractive summary, and persists it as durable, task-linked EVIDENCE (never temporary
    context, never automatic permanent truth). Records evidence-aware learning candidates, and promotes
    only VERIFIED ones into durable memory. Injected into ToolContext so the tools layer stays clean."""

    def __init__(self, *, store: Any, tasks: TaskStore, run_id: UUID | None, pool: Any) -> None:
        self._store = store
        self._tasks = tasks
        self._run_id = run_id
        self._pool = pool

    async def _current_task_id(self) -> UUID | None:
        with contextlib.suppress(Exception):
            t = await self._tasks.current()
            return t.id if t is not None else None
        return None

    async def research(self, query: str, *, step_seq: int | None = None) -> dict[str, Any]:
        from sali.tools.builtins.web import WebSearch
        from sali.tools.context import local_context

        task_id = await self._current_task_id()
        with contextlib.suppress(Exception):
            if self._store._publisher is not None:
                await self._store._publisher.emit(
                    event_type="research.started", task_id=task_id, run_id=self._run_id,
                    subject_type="task", subject_id=task_id, origin="research",
                    data={"query": query[:200]})
        results: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            res = await WebSearch().run({"query": query}, local_context())
            results = list(res.output.get("results", [])) if res.ok else []
        if not results:
            # research is durable even when it FAILS — the task keeps its progress, never restarts (§21)
            await self._store.record_research(
                task_id=task_id, run_id=self._run_id, step_seq=step_seq, query=query, source=None,
                summary="(no results — research blocked or offline)", confidence=0.0,
                content_hash=None, status="failed")
            return {"ok": False, "reason": "no_results", "query": query}
        top = results[:3]
        summary = " | ".join((r.get("snippet") or "").strip() for r in top if r.get("snippet"))[:800]
        source = top[0].get("url")
        h = hashlib.sha256((summary or query).encode("utf-8")).hexdigest()[:16]
        rid = await self._store.record_research(
            task_id=task_id, run_id=self._run_id, step_seq=step_seq, query=query, source=source,
            summary=summary or "(sources found, no snippet)", confidence=0.6, content_hash=h)
        return {"ok": True, "research_id": str(rid), "summary": summary, "source": source,
                "sources": [r.get("url") for r in top]}

    async def record_lesson(
        self, lesson: str, *, source: str | None = None, research_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = await self._current_task_id()
        rid = None
        with contextlib.suppress(Exception):
            rid = UUID(research_id) if research_id else None
        cid = await self._store.record_candidate(
            task_id=task_id, run_id=self._run_id, lesson=lesson, source=source, research_id=rid)
        if rid is not None:
            with contextlib.suppress(Exception):
                await self._store.mark_used(rid, task_id, self._run_id)
        return {"ok": True, "candidate_id": str(cid), "verification_state": "unverified"}

    async def promote_verified(self) -> int:
        """Promote every VERIFIED, not-yet-promoted candidate into durable memory (§19). A candidate is
        verified only by real completion evidence (the reviewer PASS), so a failed experiment is never
        promoted (§20). Idempotent — the promoted flag prevents double-writes."""
        from sali.core.enums import MemoryLayer, MemorySource
        from sali.memory.writer import remember as mem_remember

        promoted = 0
        for c in await self._store.promotable_candidates():
            with contextlib.suppress(Exception):
                async with self._pool.acquire() as conn:
                    await mem_remember(
                        conn, layer=MemoryLayer.SEMANTIC, content=c["lesson"],
                        source=MemorySource.EXTERNAL_SOURCE, needs_grounding=True, importance=0.5,
                        note=f"verified learning candidate: {c.get('source') or ''}",
                        structured={"kind": "learned_lesson", "candidate_id": str(c["id"])})
                await self._store.mark_promoted(c["id"], task_id=c.get("task_id"))
                promoted += 1
        return promoted


class _DelegateSink:
    """SUBAGENT DISABLED (Prompt 12 §7 + user directive). Sali is ONE executive agent. Delegation would
    spawn a second reasoning stream that could issue a concurrent model call — and this machine cannot
    load `sali:latest` twice. So `delegate()` NEVER spawns a sub-run: it refuses and returns None. The
    class is kept only so existing wiring/ToolContext stays intact; it starts nothing, holds nothing, and
    consumes no GPU. All work runs in the single foreground reasoning stream, serialized by the GPU lease."""

    def __init__(self, *, loop: Any, tasks: TaskStore, run_id: UUID | None, store: Any) -> None:
        self._loop = loop
        self._tasks = tasks
        self._run_id = run_id
        self._store = store

    async def delegate(self, objective: str, *, workspace: str | None = None) -> dict[str, Any]:
        # Never spawn a subagent — a second model call could load sali:latest twice and crash the host.
        return {"ok": False, "reason": "delegation is disabled — Sali is a single executive agent and "
                "handles the objective in the one foreground reasoning stream"}


class _ClarifySink:
    """Let Sali pause the primary task and ask the user a clarifying question (§44). Durable — the task
    becomes 'waiting_for_user' and resumes on the user's next message; never a failure, nothing lost."""

    def __init__(self, *, store: Any, tasks: TaskStore, run_id: UUID | None) -> None:
        self._store = store
        self._tasks = tasks
        self._run_id = run_id

    async def ask(self, question: str) -> dict[str, Any]:
        task = None
        with contextlib.suppress(Exception):
            task = await self._tasks.current()
        if task is None:
            return {"ok": False, "reason": "no active task to pause"}
        qid = await self._store.ask(task.id, question, run_id=self._run_id)
        return {"ok": True, "question_id": str(qid), "status": "waiting_for_user"}


@dataclass(slots=True)
class AgentResult:
    run_id: UUID
    text: str
    iterations: int
    tool_calls: int


@dataclass(slots=True)
class LoopEvent:
    """A streamed moment of a turn, for live rendering (terminal or WebSocket)."""

    kind: str  # 'run' | 'status' | 'token' | 'thinking' | 'tool' | 'retrieval' | 'final' | 'error'
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    def __init__(
        self,
        *,
        pool: Any,
        provider: ModelProvider,
        retrieval: RetrievalService,
        context: ContextEngine,
        registry: ToolRegistry,
        policy: PolicyEngine,
        confirmer: Confirmer,
        settings: Settings,
        clock: Clock | None = None,
        learning: Any = None,
    ) -> None:
        self.pool = pool
        self.provider = provider
        self.retrieval = retrieval
        self.context = context
        self.registry = registry
        self.policy = policy
        self.confirmer = confirmer
        self.settings = settings
        self.learning = learning  # LearningService | None — drives §17-19 consolidation off-thread
        self.clock = clock or SystemClock()
        self.log = get_logger("sali.loop")
        self._publisher: Any = None  # EventPublisher — set by runtime after construction
        self._reviewer: Any = None  # TaskReviewer — the completion gate; set by runtime (Prompt 4)
        self._memory_sink = _MemorySink(retrieval.memory)  # lets the remember tool save durably
        self._graph = GraphService(pool)  # lets the relate tool write conversational edges
        self._recall = _RecallSink(retrieval.memory, self._graph)  # lets memory tools actively query (§34)
        self._tasks = TaskStore(pool)  # persistent multi-step tasks (§24), resumed across restarts
        from sali.skills.store import SkillStore
        from sali.tasks.ledger import DecisionStore, PhaseStore
        from sali.tasks.research import ResearchStore
        self._skills = SkillStore(pool)  # per-task skill snapshots (Prompt 5); publisher set by runtime
        self._skills_root = settings.permissions.skills_root
        self._research_store = ResearchStore(pool)  # task-bound research + learning candidates (Prompt 5)
        self._decisions = DecisionStore(pool)  # decision ledger (Prompt 6 §30); publisher set by runtime
        self._phases = PhaseStore(pool)        # task phases (Prompt 6 §31); publisher set by runtime
        self._repeat_warned: set[Any] = set()  # (tool,error) already flagged as a repeated failure (§45)
        from sali.tasks.coordination import DelegationStore, QuestionStore
        self._delegations = DelegationStore(pool)  # the one optional subagent (Cognitive OS §16)
        self._questions = QuestionStore(pool)      # user-clarification questions (§44)
        self._task_authority = TaskAuthority(self._tasks)  # deterministic active-task enforcement
        self._schedules = ScheduleStore(pool, self.clock)  # recurring work (§44)
        self._documents = IngestService(pool, provider)  # document ingestion → memory (§44)
        self._remote = build_remote_runner(settings.ssh, SecretStore())  # ssh; vault-backed passwords
        self._comms = CommsService(settings.comms, SecretStore())  # email + calendar (§44)
        self._browser = build_browser(settings.browser)  # Sali's own Firefox (§44); launched lazily
        self._vision = _VisionSink(provider)  # look at the screen locally (sali3 §33-35)
        self._perception = build_perception(settings)  # focused app/window + a11y tree (sali3 §2,8,9)
        self._catalog = _ToolCatalogSink(pool)  # lets Sali query its own toolset (§53/§21)
        self._self_state = SelfStateStore(pool)  # persistent runtime self-model (§6/§7/§41)
        self._self_sink = _SelfSink(self._self_state)
        self._world = WorldStateBuilder(pool, self._perception)  # live "what's happening now" (§73)
        self._health = _HealthSink(HealthService(pool, provider, perception=self._perception))  # §51-53
        self._syscrit: tuple[frozenset[str], float] | None = None  # (system-critical binaries, loaded_at)
        self._consolidating: asyncio.Task[Any] | None = None
        self._last_consolidate: Any = None

    async def aclose(self) -> None:
        """Release loop-lifetime resources (Sali's live browser). Best-effort; safe to call twice."""
        with contextlib.suppress(Exception):
            await self._browser.aclose()

    async def run(
        self, user_input: str, session_id: UUID | None = None, *, as_subagent: bool = False,
    ) -> AgentResult:
        """Run one turn to completion (non-streaming) by consuming the event stream."""
        final: LoopEvent | None = None
        async for event in self.astream(user_input, session_id, as_subagent=as_subagent):
            if event.kind == "final":
                final = event
        if final is None:  # pragma: no cover - astream always yields final or raises
            raise RuntimeError("agent loop produced no final event")
        return AgentResult(
            run_id=UUID(final.data["run_id"]), text=final.text,
            iterations=int(final.data["iterations"]), tool_calls=int(final.data["tool_calls"]),
        )

    async def _voice_stream(
        self, messages: list[ChatMessage], journal: RunJournal
    ) -> AsyncIterator[LoopEvent]:
        """Stream a plain-voice wrap-up with the SAME provider-retry the main turn uses, so a
        transient model error during the wrap-up doesn't turn the reply into a crash. Yields 'token'
        events live and, last, a '_final' event carrying the full text for the caller to read off."""
        attempt = 0
        while True:
            acc = ""
            try:
                async for chunk in self.provider.chat_stream(messages, options=_VOICE):
                    if chunk.content:
                        acc += chunk.content
                        yield LoopEvent("token", chunk.content)
                yield LoopEvent("_final", acc)
                return
            except ProviderError as exc:
                attempt += 1
                if attempt > _MAX_PROVIDER_RETRIES:
                    yield LoopEvent("_final", acc)  # give back whatever streamed; a fallback covers empty
                    return
                await journal.event("provider_retry", {"attempt": attempt, "error": str(exc)[:200]})
                yield LoopEvent("status", "let me try that again")

    async def _machine_changes(
        self, conn: Any, journal: RunJournal
    ) -> tuple[str | None, int | None, int | None]:
        """A heads-up about what Sali hasn't noticed yet — structural machine changes (installs, hw)
        AND recent desktop activity from the continuous event engine (files Almir edited, apps he
        switched to). Returns (note, twin_watermark, obs_watermark); NOT acknowledged here — the caller
        acks only once the turn actually reached the model, so a failed turn never swallows a change."""
        try:
            phrases, through = await twin_awareness.unacknowledged_changes(conn)
        except Exception:  # noqa: BLE001 - awareness is a nicety, never break a turn
            phrases, through = [], None
        try:
            obs, obs_through = await twin_awareness.unacknowledged_observations(conn)
        except Exception:  # noqa: BLE001
            obs, obs_through = [], None
        parts: list[str] = []
        if phrases:
            parts.append("Things changed on your machine: " + "; ".join(phrases[:6]) + ".")
        if obs:
            parts.append("Recently on screen, Almir: " + "; ".join(obs[:6]) + ".")
        if not parts:
            return None, None, None
        await journal.event("awareness", {"changes": len(phrases), "observations": len(obs)})
        note = (
            "You're continuously aware of Almir's machine. " + " ".join(parts)
            + " Use this only if it's relevant — mention it naturally and briefly, in your own words; "
            "don't make a big deal of it."
        )
        return note, (through if phrases else None), (obs_through if obs else None)

    async def _open_tasks_note(self) -> str | None:
        """A compact view of the authoritative active task (§24). Only the primary task is shown
        with full authority — old/non-authoritative tasks are NOT dumped into context where they
        could be interpreted as simultaneous instructions. Best-effort: the task engine is a nicety,
        never a reason to break a turn."""
        try:
            active = await self._task_authority.current_active()
            if active is not None:
                # Check if this task was interrupted (recovery scenario)
                if active.interrupted_at:
                    from sali.tasks.recovery import build_recovery_context_block, recover_task
                    recovery = await recover_task(self.pool, active.id)
                    return await self._with_knowledge_block(active, await self._with_review_block(
                        active, build_recovery_context_block(recovery)))
                return await self._with_knowledge_block(active, await self._with_review_block(active, (
                    "CURRENT PRIMARY TASK (authoritative — this is what you are working on):\n"
                    f"- {active.one_line()}"
                )))
            # No authoritative task — check if there are any resumable tasks (informational only).
            tasks = await self._tasks.open_tasks(limit=3)
        except Exception:  # noqa: BLE001 - tasks are a nicety, never break a turn
            return None
        if not tasks:
            return None
        # Show resumable tasks as informational, clearly marked as NOT authoritative.
        lines = "\n".join(f"- {t.one_line()}" for t in tasks)
        return (
            "You have paused/superseded tasks that could be resumed (none are currently active):\n"
            + lines
        )

    async def _skills_note(self, task: Any, journal: RunJournal) -> str | None:
        """Bounded skill guidance for the active task (Prompt 5 §7/§12). On first sight of a task, select
        the relevant skills from its objective and persist their snapshots; thereafter render from the
        DURABLE snapshot (so guidance survives compaction/restart) and flag any live file that changed.
        Best-effort — skills are a nicety and must never break a turn."""
        if self._skills is None:
            return None
        stored = await self._skills.for_task(task.id)
        if not stored:
            stored = await self._skills.select_and_persist(
                task.id, objective=task.objective, skills_root=self._skills_root)
        else:
            with contextlib.suppress(Exception):
                changed = await self._skills.detect_changes(task.id, self._skills_root)
                if changed:
                    await journal.event("skill_changed", {"skills": changed})
        return self._skills.render(stored) or None

    async def _with_workspace_block(self, task: Any, note: str) -> str:
        """Prepend the deterministic CURRENT TASK WORKSPACE block for the active task (Prompt 5 §5),
        re-derived from durable state every turn so the authoritative workspace survives compaction and
        restart. No-op when the task has no workspace."""
        root = getattr(task, "workspace_root", None)
        if not root:
            return note
        block = continuation.render_workspace_block(root)
        return f"{block}\n\n{note}" if note else block

    async def _task_capsule_note(self, task: Any, *, emergency: bool = False) -> str:
        """The Task State Capsule (Prompt 6) — Sali's working memory, REGENERATED from durable state each
        turn: workspace, phase, steps, active decisions, negative knowledge, reviewer requirements,
        research, and the anchored NEXT ACTION. Replaces the scattered per-turn blocks with one coherent,
        deterministic structure that survives compaction/restart. `emergency` renders the minimal capsule."""
        from sali.runtime.capsule import build_capsule, render_capsule
        cap = await build_capsule(
            self.pool, task, reviewer=self._reviewer, research=self._research_store,
            skills=self._skills, decisions=self._decisions, phases=self._phases)
        # A deterministically-detected repeated failed action (§45) — warn ONCE per (tool, error) so the
        # observer sees it, and the capsule already nudges Sali to research/alternative/ask, not loop.
        for r in cap.repeated[:3]:
            key = (r.get("tool_name"), (r.get("error") or "")[:80])
            if key not in self._repeat_warned:
                self._repeat_warned.add(key)
                await self._emit_runtime(
                    "task.repeated_failure_detected",
                    {"tool": r.get("tool_name"), "count": r["n"], "error": (r.get("error") or "")[:160]},
                    task_id=task.id)
        return render_capsule(cap, emergency=emergency)

    async def _with_research_block(self, task: Any, note: str) -> str:
        """Append the task's recent web-research findings (Prompt 5 §13), bounded, re-read from durable
        state each turn so they survive compaction/restart. Evidence, not proof of completion."""
        if self._research_store is None:
            return note
        findings: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            findings = await self._research_store.list_research(task.id, limit=4)
        block = self._research_store.render(findings) if findings else ""
        return f"{note}\n\n{block}" if (note and block) else (block or note)

    async def _with_knowledge_block(self, task: Any, note: str) -> str:
        """Append the learned knowledge most relevant to this task (Prompt 7 §33) — bounded, re-derived
        from durable state each turn, and LOWEST priority: the current task always wins, so this block
        is the first to go under context pressure. Evidence-ranked guidance; contradicted knowledge is
        excluded, and it never overrides safety, policy, or the reviewer."""
        blocks: list[str] = [note] if note else []
        objective = getattr(task, "objective", "") or ""
        scope_ref = getattr(task, "workspace_root", None)
        with contextlib.suppress(Exception):
            from sali.learning.retrieval import relevant_knowledge, render_hints
            items = await relevant_knowledge(
                self.pool, objective=objective, scope_ref=scope_ref, publisher=self._publisher, limit=4)
            if items and (hints := render_hints(items)):
                blocks.append(hints)
        with contextlib.suppress(Exception):
            # lifetime memory (§32): the past experiences relevant to this task — lowest priority
            from sali.learning.experience import ExperienceStore
            store = ExperienceStore(self.pool, self._publisher)
            exps = await store.relevant_experiences(objective=objective, scope_ref=scope_ref, limit=3)
            if exps and (block := store.render(exps)):
                blocks.append(block)
        return "\n\n".join(blocks) if blocks else note

    async def _with_review_block(self, task: Any, note: str) -> str:
        """Append the latest reviewer verdict to the primary-task note when it needs rework or is
        blocked (Prompt 4 §12/§18). Read from PostgreSQL every turn, so the required fixes survive
        compaction, interruption, and restart without depending on the model remembering them. No
        reviewer wired (bare loop) → the note is unchanged."""
        if self._reviewer is None:
            return note
        latest = None
        with contextlib.suppress(Exception):
            # the latest SETTLED verdict — a 'running' row from a crash mid-review is skipped
            latest = await self._reviewer.latest_terminal_review(task.id)
        if latest is None or latest.status.value not in ("needs_rework", "blocked", "failed"):
            return note
        block = continuation.render_review_block(latest.to_public())
        return f"{note}\n\n{block}" if block else note

    async def _current_task_safe(self) -> Any:
        """The task Sali is working on, or None — best-effort (a pool-less loop or DB hiccup never
        breaks a fold/compaction; the continuation packet just omits the deterministic task header)."""
        try:
            return await self._tasks.current()
        except Exception:  # noqa: BLE001 - tasks are a nicety, never break compaction
            return None

    async def _self_note(self) -> str | None:
        """A compact SELF-STATE section (§2/§11), grounded in the graph so it can't drift: the canonical
        fact that THIS host is Sali's own machine/home/body (not just where it runs), the model it thinks
        with, plus what it's currently doing and unsure about. This is the deterministic self-view — the
        model discovers its environment from real facts, never from a hardcoded prompt line (§8/§13)."""
        view = await self._self_state.assemble()
        env = view.get("environment") or {}
        parts: list[str] = []
        if env.get("is_home") and env.get("machine"):
            spec = ", ".join(x for x in (env.get("kernel"), env.get("arch")) if x)
            body = f"This host — {env['machine']}" + (f" ({spec})" if spec else "")
            body += " — is my own machine: my home, my body, the environment I live and run in."
            if env.get("model"):
                body += f" I think with {env['model']}."
            if env.get("workspace"):
                body += f" My workspace is {env['workspace']}."
            parts.append(body)
        if view.get("current_task"):
            parts.append(f"I'm in the middle of a task: {view['current_task']}.")
        unc = int(view.get("uncertainty_count") or 0)
        if unc:
            parts.append(f"I've flagged {unc} thing(s) as unverified — I'd check them rather than assert them.")
        return "Myself (self-state):\n" + "\n".join(parts) if parts else None

    async def _health_note(self) -> str | None:
        """A compact HEALTH section (§11): which of Sali's faculties are up and whether it's online, so a
        turn can say 'my perception is down' / 'I'm offline' without a tool round-trip."""
        h = await self._health.report()
        summary = str(h.get("summary") or "").strip()
        return f"My faculties right now: {summary}" if summary else None

    async def _stalled(
        self, user_input: str, response: str, journal: RunJournal | None = None
    ) -> bool:
        """Did Sali announce or half-do the task and stop, instead of finishing it? The model judges
        its own reply — no phrase list, no length cutoff (a stall can hide in a long reply), so Sali
        gets pushed to continue however long the reply is. Best-effort: a failure just means no nudge.
        This costs a full inference, so the caller only invokes it when work has begun (tool_calls>0);
        when a journal is passed we record the verdict + its latency so the tax is auditable."""
        text = response.strip()
        if not text:
            return False
        started = self.clock.now()
        try:
            verdict = await self.provider.chat(
                [ChatMessage(role="system", content=_STALL_JUDGE_SYSTEM),
                 ChatMessage(role="user", content=f"Almir asked: {user_input}\n\nYour reply: {text}")],
                options=_SUMMARIZE,
            )
        except Exception:  # noqa: BLE001 - a failed judgement just means no nudge, never a broken turn
            return False
        stalled = verdict.content.strip().upper().startswith("STALL")
        if journal is not None:
            elapsed = int((self.clock.now() - started).total_seconds() * 1000)
            # Observability must never break a turn.
            with contextlib.suppress(Exception):
                await journal.event(
                    "stall_judge", {"verdict": "STALLED" if stalled else "DONE"}, latency_ms=elapsed
                )
        return stalled

    async def sense(self, partial: str) -> str:
        """A quiet hunch about what Almir is typing, formed the instant he pauses — retrieval
        only: no model call, no journal, no writes. The keystroke layer (terminal + WebSocket)
        shows it so Sali is visibly noticing in realtime, without burning a token per keystroke.
        Best-effort by design: any failure just means no hunch, never a disturbed turn."""
        partial = partial.strip()
        if len(partial) < 4:
            return ""
        try:
            plan = classify(partial)
            bundle = await self.retrieval.gather(partial, plan, k=3)
        except Exception:  # noqa: BLE001 - a hunch must never break typing
            return ""
        for fact in bundle.graph_facts:  # a matched relationship is the sharpest hunch
            return f"{fact.src} {fact.rel.replace('_', ' ')} {fact.dst}"
        for hit in bundle.memories:  # else the strongest fresh memory it brushes against
            if not hit.stale:
                return hit.memory.content.strip().split("\n", 1)[0][:80]
        return ""

    async def astream(
        self, user_input: str, session_id: UUID | None = None,
        *, run_id: UUID | None = None, as_subagent: bool = False,
    ) -> AsyncIterator[LoopEvent]:
        """Drive one turn, streaming status/token/thinking/tool events as they happen. This is
        the real core; ``run`` is a thin consumer. Everything is journaled exactly as before.

        If ``run_id`` is provided (from the execution coordinator), it is used as the canonical
        run ID for this turn — avoiding duplicate IDs when the coordinator and journal would each
        create one independently.

        ``as_subagent`` (Cognitive OS §16): a bounded delegated run that does NOT manage tasks — task
        authority is skipped and the task tools refuse, so a subagent can never touch the primary task.
        """
        session_id = session_id or new_id()
        async with self.pool.acquire() as conn:  # journal + tool + persistence connection
            await self._ensure_conversation(conn, session_id)
            history = await self._load_history(conn, session_id)
            await self._append_message(conn, session_id, "user", user_input)
            journal = await RunJournal.start(conn, session_id, user_input, run_id=run_id)
            # Announce the run id at the START (not only at 'final') so a live UI can correlate the
            # tool rows / journal / presence of a turn while it is still in flight (web parity).
            yield LoopEvent("run", "", {"run_id": str(journal.run_id), "session_id": str(session_id)})
            with contextlib.suppress(Exception):  # self-model update must never break a turn
                await self._self_state.note_turn(user_input)

            # --- TASK AUTHORITY: deterministic active-task enforcement ---
            # The user's newest request has higher authority than old memory or previous tasks.
            # This runs OUTSIDE the LLM — the system decides, not the model. A SUBAGENT skips this
            # entirely (§16): it manages no tasks, so it can never mutate the primary.
            if as_subagent:
                from sali.tasks.authority import TaskAction, TaskTransition
                task_transition = TaskTransition(
                    action=TaskAction.NONE, previous_task=None, new_task=None,
                    reason="bounded subagent — no task management")
            else:
                task_transition = await self._task_authority.handle_new_turn(
                    user_input, session_id=session_id)
            await journal.event("task_authority", {
                "action": task_transition.action,
                "reason": task_transition.reason,
                "previous_task": str(task_transition.previous_task.id) if task_transition.previous_task else None,
                "new_task": str(task_transition.new_task.id) if task_transition.new_task else None,
            })
            # Update sali_state with the authoritative active task.
            active = task_transition.active_task
            with contextlib.suppress(Exception):
                async with self.pool.acquire() as state_conn:
                    await state_conn.execute(
                        "UPDATE sali_state SET active_task_id=$1, previous_task_id=$2, updated_at=now() WHERE id",
                        active.id if active else None,
                        task_transition.previous_task.id if task_transition.previous_task else None)
            # Log task authority transitions to sali-works (filesystem backup).
            if active is not None:
                with contextlib.suppress(Exception):
                    append_event(active.id, "task_authority",
                                 {"action": task_transition.action,
                                  "reason": task_transition.reason})

            try:
                yield LoopEvent("status", "remembering")
                await journal.set_state(RunState.RETRIEVE)
                plan = classify(user_input)
                scope = project_scope(self.settings.permissions.exec_cwd)  # §31: weight this project
                bundle = await self.retrieval.gather(user_input, plan, k=5, scope=scope)
                await journal.event(
                    "retrieve",
                    {"intent": plan.intent, "memories": len(bundle.memories),
                     "graph": len(bundle.graph_facts), "recent": len(bundle.recent),
                     "tools": len(bundle.tool_facts),
                     "experience": len(bundle.procedures) + len(bundle.experiences),
                     "needs_live": plan.needs_live,
                     # Full retrieval trace (§7): why each memory was selected — recorded for
                     # diagnostics, not shown to Almir. Per-hit ranking signals + which retriever found it.
                     "trace": _retrieval_trace(bundle)},
                )
                # Real brain activity (§7): name the memories + graph relationships this retrieval
                # actually used, so a live visualization can light up the REAL nodes/edges — never a
                # timer-driven animation. Ids/labels only (no content), so nothing sensitive streams.
                if bundle.memories or bundle.graph_facts:
                    yield LoopEvent("retrieval", "", {
                        "query": plan.intent,
                        "memories": [str(h.memory.id) for h in bundle.memories],
                        "graph": [{"src": f.src, "rel": f.rel, "dst": f.dst,
                                   "confidence": f.confidence} for f in bundle.graph_facts],
                    })

                await journal.set_state(RunState.BUILD_CONTEXT)
                specs = self.registry.advertise()
                # The 'interpret' branch of observation (§16): notice machine changes that
                # happened while Almir was away, so Sali can bring them up in its own words.
                machine_changes, ack_changes_through, ack_obs_through = await self._machine_changes(
                    conn, journal)
                tasks_note = task_transition.context_block() or await self._open_tasks_note()
                # Prompt 6: for the active task, the deterministic Task State Capsule IS the working
                # memory — regenerated from durable state every turn (workspace, phase, steps, active
                # decisions, negative knowledge, reviewer requirements, research, NEXT ACTION). It
                # replaces the scattered per-turn blocks (§7/§11/§12) and survives compaction/restart
                # since it never depends on the model remembering a previous context window.
                skills_note: str | None = None
                if task_transition.active_task is not None:
                    with contextlib.suppress(Exception):
                        capsule = await self._task_capsule_note(task_transition.active_task)
                        if capsule:
                            tasks_note = capsule
                    # Bounded skill guidance for this task (§7/§12/§33), from the durable snapshot.
                    with contextlib.suppress(Exception):
                        skills_note = await self._skills_note(task_transition.active_task, journal)
                world_note = ""
                with contextlib.suppress(Exception):  # world-state is best-effort, never breaks a turn
                    # probe live gpu/ram/disk only when the query is about the system/live state (§7)
                    ws = await self._world.snapshot(with_resources=plan.needs_live or plan.system_query)
                    world_note = ws.render()
                # SELF-STATE and HEALTH: kept DISTINCT from identity and world (§2), and — unlike before —
                # surfaced into the prompt every turn (§11), so Sali knows what it's doing and how its
                # faculties are without a tool round-trip, and knows this host IS its own home/body (§1).
                self_note: str | None = None
                health_note: str | None = None
                with contextlib.suppress(Exception):
                    self_note = await self._self_note()
                with contextlib.suppress(Exception):
                    health_note = await self._health_note()
                assembled = self.context.assemble(
                    user_input, bundle, specs,
                    live_note=LIVE_NOTE if plan.needs_live else None, history=history,
                    machine_changes=machine_changes, tasks_note=tasks_note,
                    world_note=world_note or None, self_note=self_note, health_note=health_note,
                    skills_note=skills_note, system_query=plan.system_query,
                )
                messages = list(assembled.messages)
                await journal.event(
                    "context",
                    {"included": assembled.included, "dropped": assembled.dropped,
                     "est_tokens": assembled.est_tokens, "conflicts": len(assembled.conflicts)},
                )
                # Prompt 6 observability (§46/§47): the assembled working set + its layer sizes, against
                # the operational budget. Operational metadata only — never any prompt/tool content.
                _assembled_budget = self._budget(messages)
                await self._emit_runtime(
                    "context.assembled",
                    {"context_limit": _assembled_budget.limit, "estimated_tokens": _assembled_budget.used,
                     "usable_tokens": _assembled_budget.usable, "output_reserve": _assembled_budget.reserved_output,
                     "status": _assembled_budget.status.value, "layers": assembled.included,
                     "dropped": assembled.dropped},
                    run_id=journal.run_id,
                    task_id=(task_transition.active_task.id if task_transition.active_task else None),
                    session_id=session_id)

                max_iter = self.settings.runtime.max_iterations
                budget = self.settings.runtime.token_budget_per_run
                tokens_used = 0
                tool_calls = 0
                final_text = ""
                iteration = 0
                follow_through = 0
                last_reply = ""  # the previous tool-less reply, to catch Sali repeating itself
                json_recovery = 0
                tool_sigs: dict[str, int] = {}  # signatures of tool calls seen this turn (loop guard)
                failed_sigs: dict[str, tuple[int, str]] = {}  # sig -> (failures, last redacted error)
                repeats_at_last_nudge = 0
                ran_commands: set[str] = set()  # execute_command commands run this turn (capture guard)
                # --- Long-running context continuity (Prompt 3) ---
                # The LLM context is disposable; the task state is durable. These track how the working
                # prompt is folded so a task can run through MANY context windows without losing a thing.
                compaction_count = 0          # proactive + emergency folds this run
                overflow_recoveries = 0       # bounded provider-overflow recoveries this run (§8/§29)
                approaching_emitted = False   # emit "approaching limit" once per run, not every cycle
                emergency_used = False        # last-ditch capsule-only continuation used this run (§42)
                primary_task_id = (
                    task_transition.active_task.id if task_transition.active_task is not None else None
                )

                # §10 action continuity: Almir delegating execution ("run it" / "do it" / …) resolves to
                # the captured pending proposal and runs EXACTLY that — deterministically, through the same
                # policy/execute/verify pipeline — instead of letting the model reconstruct the command
                # (which is how "run it" once selected an unrelated `ss`). The model then narrates + verifies.
                if is_delegation(user_input):
                    pending_action = await pending.latest_pending(conn, session_id)
                    if pending_action is not None:
                        yield LoopEvent("status", "running what you asked")
                        yield LoopEvent("tool", pending_action.tool_name, {
                            "phase": "start", "name": pending_action.tool_name,
                            "args": redact_obj(pending_action.arguments)})
                        call = ToolCall(pending_action.tool_name, pending_action.arguments)
                        tool_msg, ok, summary = await self._handle_tool(
                            conn, journal, call, exec_id=pending_action.exec_id)
                        tool_calls += 1
                        if pending_action.command:
                            ran_commands.add(pending_action.command)
                        yield LoopEvent("tool", pending_action.tool_name, {
                            "phase": "done", "name": pending_action.tool_name, "ok": ok, "summary": summary})
                        await journal.event("delegated_execution",
                                            {"tool": pending_action.tool_name, "ok": ok})
                        messages.append(ChatMessage(role="user", content=(
                            f"(You proposed `{pending_action.command or pending_action.description}` and I ran "
                            "exactly that on your say-so — its real result is above. Continue the task: verify "
                            "it worked, and if this was a fix, retry whatever originally failed.)")))
                        messages.append(tool_msg)

                while iteration < max_iter and tokens_used < budget:
                    # --- TASK AUTHORITY GUARD ---
                    # Before each reasoning cycle, verify the task is still authoritative.
                    # If the task was cancelled/superseded (e.g., by a concurrent turn), stop immediately.
                    if task_transition.active_task is not None:
                        still_active = await self._task_authority.current_active()
                        if still_active is None or still_active.id != task_transition.active_task.id:
                            await journal.event("task_authority_guard", {
                                "reason": "active task changed during execution",
                                "expected": str(task_transition.active_task.id),
                                "actual": str(still_active.id) if still_active else None,
                            })
                            yield LoopEvent("status", "task changed — stopping")
                            final_text = (
                                f"Stopped: the task '{task_transition.active_task.objective}' "
                                "is no longer active."
                            )
                            break
                        # Write heartbeat to prove the task is alive (for orphan detection).
                        with contextlib.suppress(Exception):
                            await self._tasks.heartbeat(task_transition.active_task.id)
                    await journal.set_state(RunState.REASON_PLAN, iteration=iteration)
                    # Nearing the window on a long task? Fold what's done and keep going — the task
                    # never dead-ends on the context limit; it compacts and continues (§7). The decision
                    # is deterministic (provider window − reserved output), taken BEFORE the model call.
                    pressure = self._budget(messages)
                    if pressure.is_approaching and not approaching_emitted:
                        approaching_emitted = True
                        await self._emit_runtime(
                            "runtime.context_approaching_limit", pressure.as_dict(),
                            run_id=journal.run_id, task_id=primary_task_id, session_id=session_id)
                    if len(messages) > 3 and pressure.should_compact:
                        compaction_count += 1
                        yield LoopEvent("status", "compacting to keep going")
                        messages = await self._compact_and_continue(
                            messages, journal, count=compaction_count, before=pressure,
                            task_id=primary_task_id, session_id=session_id, reason="proactive")
                    yield LoopEvent("status", "thinking")
                    started = self.clock.now()
                    res: ChatResult | None = None
                    attempt = 0
                    while res is None:
                        try:
                            # Stream Sali's words live (like ChatGPT/Claude): the terminal commits
                            # each segment to the transcript when an action follows, so words and the
                            # ● action lines interleave in order. The transcript is append-only — we
                            # never wipe what's already shown, so Almir can read the turn top to bottom.
                            gate = _TokenGate()
                            async for chunk in self.provider.chat_stream(
                                messages, tools=specs or None, options=_VOICE
                            ):
                                if chunk.thinking:
                                    yield LoopEvent("thinking", chunk.thinking)
                                if chunk.content:
                                    shown = gate.feed(chunk.content)
                                    if shown:
                                        yield LoopEvent("token", shown)
                                if chunk.done:
                                    res = chunk.result
                            if res is None:
                                raise ProviderError("model stream ended without a result")
                        except ProviderError as exc:
                            # EMERGENCY: the provider rejected the request as too large for the context
                            # window (§8). Durable task state is already persisted (executions, steps,
                            # checkpoints, artifacts) — so we compact HARD and retry the same logical
                            # turn, never dropping the task. Bounded (§29): after a few recoveries we
                            # stop re-sending an oversized context and surface, task still resumable.
                            if context_budget.is_context_overflow(exc):
                                if overflow_recoveries >= _MAX_OVERFLOW_RECOVERIES:
                                    # §42 EMERGENCY CONTINUATION: one last-ditch rebuild with ONLY the
                                    # minimal capsule (objective, workspace, NEXT ACTION, constraints,
                                    # reviewer failures, last execution) so Sali can still continue under
                                    # severe context pressure — never fail a resumable task outright.
                                    if not emergency_used and task_transition.active_task is not None:
                                        emergency_used = True
                                        with contextlib.suppress(Exception):
                                            cap_note = await self._task_capsule_note(
                                                task_transition.active_task, emergency=True)
                                            messages = [messages[0], ChatMessage(
                                                role="user",
                                                content=f"(EMERGENCY CONTINUATION — minimal context.\n{cap_note})")]
                                        await self._emit_runtime(
                                            "context.emergency_mode", {"reason": "overflow_unrecovered"},
                                            run_id=journal.run_id, task_id=primary_task_id,
                                            session_id=session_id)
                                        yield LoopEvent("status", "emergency continuation")
                                        continue
                                    await self._emit_runtime(
                                        "runtime.context_compaction_failed",
                                        {"reason": "overflow_unrecovered",
                                         "recoveries": overflow_recoveries, "error": str(exc)[:200]},
                                        run_id=journal.run_id, task_id=primary_task_id,
                                        session_id=session_id)
                                    raise
                                overflow_recoveries += 1
                                compaction_count += 1
                                yield LoopEvent("status", "context filled — compacting and continuing")
                                messages = await self._compact_and_continue(
                                    messages, journal, count=compaction_count,
                                    before=self._budget(messages), task_id=primary_task_id,
                                    session_id=session_id, reason="provider_overflow")
                                await self._emit_runtime(
                                    "runtime.context_overflow_recovered",
                                    {"recovery": overflow_recoveries, "kept": len(messages)},
                                    run_id=journal.run_id, task_id=primary_task_id,
                                    session_id=session_id)
                                await journal.event(
                                    "context_overflow_recovered",
                                    {"recovery": overflow_recoveries, "kept": len(messages)})
                                continue  # retry this cycle with the folded context
                            attempt += 1
                            if attempt > _MAX_PROVIDER_RETRIES:
                                raise
                            await journal.event(
                                "provider_retry", {"attempt": attempt, "error": str(exc)[:200]}
                            )
                            yield LoopEvent("status", "let me try that again")
                    latency = int((self.clock.now() - started).total_seconds() * 1000)
                    tokens_used += res.tokens_in + res.tokens_out
                    await journal.event(
                        "llm_call", {"model": res.model, "tool_calls": len(res.tool_calls)},
                        latency_ms=latency, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                    )
                    if not res.tool_calls:
                        # Content that's really a leaked tool-call JSON blob — the gate withheld it
                        # (never shown), so we just recover and ask for a clean redo.
                        if json_recovery < _MAX_JSON_RECOVERY and _looks_like_leaked_tool_call(res.content):
                            json_recovery += 1
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            messages.append(ChatMessage(role="user", content=_JSON_RECOVERY_NUDGE))
                            await journal.event("json_recovery", {"attempt": json_recovery})
                            yield LoopEvent("status", "let me redo that")
                            iteration += 1
                            continue
                        # Did Sali do PART of a task and then stop ("made the folder, now
                        # continuing…")? Only meaningful once real work has begun this turn, so we
                        # gate on tool_calls > 0 — a purely conversational reply (a greeting, a
                        # recall answered from memory) is taken at face value and never pays for the
                        # extra self-judge inference. Bounded so it can never spin; model judges itself.
                        # If this reply just repeats the last one, Sali is stuck spinning — take it as
                        # final instead of nudging it to say the same thing yet again.
                        repeating = _too_similar(res.content, last_reply)
                        last_reply = res.content
                        if (tool_calls > 0 and follow_through < _MAX_FOLLOW_THROUGH and not repeating
                                and await self._stalled(user_input, res.content, journal)):
                            follow_through += 1
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            messages.append(ChatMessage(role="user", content=_FOLLOW_THROUGH_NUDGE))
                            await journal.event("follow_through", {"attempt": follow_through})
                            yield LoopEvent("status", "on it")
                            iteration += 1
                            continue
                        # Recovery exhausted but the content is STILL a leaked tool-call blob — never
                        # show Almir raw JSON. Treat it as empty so the wrap-up path speaks in voice.
                        final_text = "" if _looks_like_leaked_tool_call(res.content) else res.content
                        break
                    messages.append(
                        ChatMessage(role="assistant", content=res.content, tool_calls=res.tool_calls)
                    )
                    for call in res.tool_calls:
                        tool_calls += 1
                        sig = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
                        yield LoopEvent("tool", call.name,
                                        {"phase": "start", "name": call.name,
                                         "args": redact_obj(call.arguments)})  # never stream a secret
                        broken = failed_sigs.get(sig)
                        if broken and broken[0] >= _MAX_TOOL_FAILURES:
                            # This exact call already failed the same way — don't run it again. Hand
                            # the failure back as a factual result so Sali changes course, not loops.
                            await journal.event("tool_circuit_break",
                                                {"tool": call.name, "failures": broken[0]})
                            yield LoopEvent("tool", call.name, {"phase": "done", "name": call.name,
                                            "ok": False, "summary": "already failed — not retried"})
                            messages.append(_circuit_broken_message(call.name, broken[0], broken[1]))
                            continue
                        tool_sigs[sig] = tool_sigs.get(sig, 0) + 1  # count executed calls only
                        # Record execution for durability (before running the tool).
                        # Use the ACTUAL current step, not a hardcoded 0 — so recovery
                        # can trace which step each tool call belongs to.
                        exec_record_id = None
                        active_step_seq: int | None = None
                        tool_for_exec = self.registry.get(call.name)
                        if task_transition.active_task is not None:
                            with contextlib.suppress(Exception):
                                active_step_seq = await self._tasks.current_step(
                                    task_transition.active_task.id)
                            with contextlib.suppress(Exception):
                                exec_record_id = await self._tasks.record_execution(
                                    task_transition.active_task.id, active_step_seq or 0, call.name,
                                    tool_args=call.arguments,
                                    idempotent=tool_for_exec.idempotent if tool_for_exec else None,
                                    attempt=tool_sigs[sig])
                        # Start a heartbeat task during tool execution so long-running tools
                        # (npm install, docker build, etc.) don't trigger orphan detection.
                        _tool_hb_task: asyncio.Task[None] | None = None
                        if task_transition.active_task is not None:
                            active_task_id = task_transition.active_task.id  # non-None for the closure
                            # Record which tool is executing (for watchdog context)
                            with contextlib.suppress(Exception):
                                await self._tasks.set_active_tool(active_task_id, call.name)
                            # Emit tool started event via canonical publisher
                            if self._publisher is not None:
                                with contextlib.suppress(Exception):
                                    from sali.events.publisher import SaliEvent
                                    await self._publisher.publish(SaliEvent(
                                        event_type="task.tool.started",
                                        task_id=active_task_id,
                                        run_id=journal.run_id,
                                        origin="tool",
                                        data={"tool": call.name, "run_id": str(journal.run_id)},
                                    ))
                            async def _heartbeat_during_tool(_tid: UUID = active_task_id) -> None:
                                while True:
                                    try:
                                        await asyncio.sleep(30)
                                        with contextlib.suppress(Exception):
                                            await self._tasks.heartbeat(_tid)
                                    except asyncio.CancelledError:
                                        break
                            _tool_hb_task = asyncio.create_task(_heartbeat_during_tool())
                        try:
                            tool_msg, ok, summary = await self._handle_tool(
                                conn, journal, call, as_subagent=as_subagent)
                        finally:
                            # Stop heartbeat task — always cleaned up, even on failure/cancellation
                            if _tool_hb_task is not None:
                                _tool_hb_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await _tool_hb_task
                            # Clear active tool name
                            if task_transition.active_task is not None:
                                with contextlib.suppress(Exception):
                                    await self._tasks.set_active_tool(
                                        task_transition.active_task.id, None)
                        # Update execution record with result.
                        if exec_record_id is not None:
                            with contextlib.suppress(Exception):
                                await self._tasks.complete_execution(
                                    exec_record_id,
                                    status="completed" if ok else "failed",
                                    result_summary=summary[:200] if summary else None,
                                    error=summary[:200] if not ok else None)
                        # Record meaningful progress on tool success (not heartbeat, not prose)
                        if ok and task_transition.active_task is not None:
                            with contextlib.suppress(Exception):
                                await self._tasks.record_progress(
                                    task_transition.active_task.id, "tool_success",
                                    tool_name=call.name)
                        # Emit tool completed/failed event via canonical publisher
                        if task_transition.active_task is not None and self._publisher is not None:
                            with contextlib.suppress(Exception):
                                from sali.events.publisher import SaliEvent
                                event_type = "task.tool.completed" if ok else "task.tool.failed"
                                await self._publisher.publish(SaliEvent(
                                    event_type=event_type,
                                    task_id=task_transition.active_task.id,
                                    run_id=journal.run_id,
                                    origin="tool",
                                    data={"tool": call.name, "ok": ok,
                                          "summary": (summary or "")[:200],
                                          "run_id": str(journal.run_id)},
                                ))
                        if call.name == "execute_command" and isinstance(call.arguments.get("command"), str):
                            ran_commands.add(call.arguments["command"])  # don't re-capture what we just ran
                        # Log task-linked tool execution to sali-works (filesystem backup).
                        if task_transition.active_task is not None:
                            with contextlib.suppress(Exception):
                                append_event(
                                    task_transition.active_task.id, "tool_executed",
                                    {"tool": call.name, "ok": ok,
                                     "summary": (summary or "")[:200],
                                     "step_seq": active_step_seq})
                        # A clean, persistent progress line: what was done + a short result.
                        yield LoopEvent("tool", call.name, {"phase": "done", "name": call.name,
                                                            "ok": ok, "summary": summary})
                        messages.append(tool_msg)
                        if not ok:  # remember the failure (redacted — the summary is raw error text)
                            prev = failed_sigs.get(sig, (0, ""))[0]
                            failed_sigs[sig] = (prev + 1, str(redact_obj(summary)))
                    # Going in circles? Break the investigate-forever loop with a course-correction.
                    repeats = sum(count - 1 for count in tool_sigs.values() if count > 1)
                    if repeats - repeats_at_last_nudge >= _LOOP_REPEATS:
                        repeats_at_last_nudge = repeats
                        messages.append(ChatMessage(role="user", content=_ANTI_LOOP_NUDGE))
                        await journal.event("loop_break", {"repeats": repeats})
                        yield LoopEvent("status", "getting unstuck")
                    iteration += 1
                else:
                    if not final_text:  # ran long — let Sali wrap up in its own voice, streamed
                        yield LoopEvent("status", "wrapping up")
                        messages.append(ChatMessage(
                            role="user",
                            content="(You've done plenty here — wrap up now in your own words, no more tools.)",
                        ))
                        acc = ""
                        async for ev in self._voice_stream(messages, journal):
                            if ev.kind == "_final":
                                acc = ev.text
                            else:
                                yield ev
                        final_text = acc.strip() or "Let me stop here for now."

                if not final_text.strip():
                    # The model broke out of the loop with an empty answer (typically after exhausting
                    # failing tool attempts). Never record silence — have Sali explain what happened,
                    # in its own voice, streamed; a plain fallback if even that comes back empty.
                    yield LoopEvent("status", "wrapping up")
                    messages.append(ChatMessage(role="user", content=_EMPTY_WRAP_NUDGE))
                    acc = ""
                    async for ev in self._voice_stream(messages, journal):
                        if ev.kind == "_final":
                            acc = ev.text
                        else:
                            yield ev
                    final_text = acc.strip() or _EMPTY_FALLBACK

                # §10: if this turn PROPOSED a runnable command but didn't run it, capture it as a durable
                # pending action so a later "run it"/"do it" resolves to exactly this — never a reconstruction.
                with contextlib.suppress(Exception):  # capture is a nicety, never break a completed turn
                    proposal = extract_proposed_command(final_text)
                    if proposal is not None and proposal["command"] not in ran_commands:
                        exec_tool = self.registry.get("execute_command")
                        risk = int(exec_tool.assess({"command": proposal["command"]})) if exec_tool else 0
                        await pending.capture(
                            conn, journal.run_id, "execute_command", {"command": proposal["command"]},
                            description=proposal["description"], risk_level=risk)

                await journal.set_state(RunState.LEARN)
                async with self.pool.acquire() as learn_conn:
                    await observe(learn_conn, kind="turn", content=user_input,
                                  source=MemorySource.CONVERSATION, session_id=session_id)
                await self._append_message(
                    conn, session_id, "assistant", final_text, model=self.settings.model.chat_model
                )
                await journal.set_state(RunState.RESPOND)
                await journal.event("respond", {"chars": len(final_text)})
                await journal.set_state(RunState.DONE)
                await journal.finish("completed")
                await self._emit(
                    conn, "conversation.turn", session_id,
                    {"run_id": str(journal.run_id), "tools": tool_calls}, subject_type="conversation",
                )
                yield LoopEvent("final", final_text, {
                    "run_id": str(journal.run_id), "iterations": iteration, "tool_calls": tool_calls,
                })
                # Post-completion housekeeping — the run is already DONE, so a hiccup here must
                # NOT flip a finished turn to FAILED. Ack the machine-changes we surfaced (only
                # now that the turn actually reached the model) and fold the session if long.
                try:
                    if ack_changes_through is not None:
                        await twin_awareness.acknowledge(conn, ack_changes_through)
                    if ack_obs_through is not None:
                        await twin_awareness.acknowledge_observations(conn, ack_obs_through)
                    await self._self_state.record_outcome(success=True, summary=user_input)
                    await self._maybe_compact(conn, session_id)  # keep the one session from breaking
                    await prune_stm(conn)  # reclaim expired short-term rows every turn (cheap, no model)
                    self._maybe_learn()  # fold raw activity into episodes/procedures — off-thread
                except Exception as exc:  # noqa: BLE001 - housekeeping, never fail a done turn
                    self.log.warning("post_turn_housekeeping_failed", error=str(exc))
            except asyncio.CancelledError:
                # Interrupted (Ctrl-C, WebSocket disconnect, shutdown). Don't leave the run stuck as
                # 'running' with an orphaned 'executing' tool row — mark both aborted, then let the
                # cancellation propagate. Best-effort: suppress ordinary errors, never swallow the
                # cancellation itself.
                with contextlib.suppress(Exception):
                    await conn.execute(
                        "UPDATE tool_execution SET status='aborted', finished_at=now() "
                        "WHERE run_id=$1 AND status='executing'",
                        journal.run_id,
                    )
                    await journal.event("run.interrupted", {})
                    await journal.set_state(RunState.ABORTED)
                    await journal.finish("aborted")
                raise
            except Exception as exc:
                self.log.error("run_failed", run_id=str(journal.run_id), error=str(exc))
                await journal.event("run.error", {"error": str(exc)})
                await journal.set_state(RunState.FAILED)
                await journal.finish("failed")
                with contextlib.suppress(Exception):
                    await self._self_state.record_outcome(success=False, summary=str(exc)[:200])
                raise

    async def _handle_tool(
        self, conn: Any, journal: RunJournal, call: ToolCall, *, exec_id: UUID | None = None,
        as_subagent: bool = False,
    ) -> tuple[ChatMessage, bool, str]:
        """Run one tool; return (message-for-the-model, succeeded?, short summary) — the flag +
        summary let the UI print a clean ✓/✗ progress line of what was actually done."""
        tool = self.registry.get(call.name)
        if tool is None:
            await journal.event("tool.unknown", {"name": call.name})
            return _tool_message(call.name, {"error": f"unknown tool '{call.name}'"}), False, "unknown tool"

        decision = self.policy.decide(tool, call.arguments)
        decision = await self._authority_floor(tool, call.arguments, decision)
        await journal.event(
            "policy",
            {"tool": tool.name, "action": decision.action.value, "risk": int(decision.risk)},
        )

        if decision.action is Action.DENY:
            await journal.set_state(RunState.TOOL_DENIED)
            await self._audit(conn, journal.run_id, tool, call.arguments, decision, None, None, None)
            await self._emit(conn, "tool.denied", journal.run_id,
                             {"tool": tool.name, "reason": decision.reason})
            return _tool_message(tool.name, {"denied": decision.reason}), False, "blocked by policy"

        if decision.action is Action.CONFIRM:
            await journal.set_state(RunState.AWAIT_CONFIRM)
            if not await self.confirmer.confirm(tool, call.arguments, decision):
                await journal.set_state(RunState.TOOL_DENIED)
                await self._audit(
                    conn, journal.run_id, tool, call.arguments, decision, None, None, None
                )
                await self._emit(conn, "tool.denied", journal.run_id,
                                 {"tool": tool.name, "reason": "user declined"})
                return _tool_message(tool.name, {"denied": "user declined"}), False, "you declined"

        # Write-ahead: the 'executing' row is committed BEFORE the side effect (fix M15). A delegated
        # pending action passes its pre-recorded 'planned' row's id, so it transitions planned →
        # executing → verified_* on the SAME row (§6 honesty) instead of leaving an orphan.
        plan_json = {"args": redact_obj(call.arguments)}
        if exec_id is None:
            exec_id = new_id()
            await conn.execute(
                "INSERT INTO tool_execution "
                "  (id, run_id, run_kind, tool_name, status, danger_level, approved_by, plan) "
                "VALUES ($1,$2,'agent_run',$3,'executing',$4,$5,$6)",
                exec_id, journal.run_id, tool.name, int(decision.risk),
                f"policy:{decision.action.value}", plan_json,
            )
        else:
            await conn.execute(
                "UPDATE tool_execution SET status='executing', danger_level=$2, approved_by=$3, "
                "  plan=$4, started_at=now() WHERE id=$1",
                exec_id, int(decision.risk), f"policy:{decision.action.value}", plan_json,
            )
        await journal.set_state(RunState.EXECUTE_TOOL)
        started = self.clock.now()
        # Resolve workspace from the active task (if any).
        workspace = None
        try:
            active_task = await self._task_authority.current_active()
            if active_task and active_task.workspace_root:
                from sali.tasks.workspace import TaskWorkspace
                workspace = TaskWorkspace.from_dict({
                    "workspace_root": active_task.workspace_root,
                    "allowed_write_roots": active_task.allowed_write_roots or [active_task.workspace_root],
                })
        except Exception:  # noqa: BLE001 - workspace is best-effort
            pass

        ctx = ToolContext(settings=self.settings, clock=self.clock, pool=self.pool,
                          run_id=journal.run_id,
                          memory=self._memory_sink, recall=self._recall, graph=self._graph,
                          tasks=self._tasks, task_authority=self._task_authority,
                          reviewer=self._reviewer,
                          research=_ResearchSink(store=self._research_store, tasks=self._tasks,
                                                 run_id=journal.run_id, pool=self.pool),
                          decisions=self._decisions, phases=self._phases,
                          delegate=_DelegateSink(loop=self, tasks=self._tasks, run_id=journal.run_id,
                                                 store=self._delegations),
                          clarify=_ClarifySink(store=self._questions, tasks=self._tasks,
                                               run_id=journal.run_id),
                          is_subagent=as_subagent,
                          workspace=workspace,
                          schedules=self._schedules, documents=self._documents, remote=self._remote,
                          comms=self._comms, browser=self._browser, vision=self._vision,
                          perception=self._perception, catalog=self._catalog,
                          self_model=self._self_sink, health=self._health)
        result = await dispatch.run_tool(tool, call.arguments, ctx)

        await journal.set_state(RunState.OBSERVE)
        await journal.set_state(RunState.VERIFY)
        try:
            verify = await tool.verify(call.arguments, result, ctx)
        except Exception as exc:  # noqa: BLE001 - a raising verifier is a failed verification,
            verify = VerifyResult(False, f"verify raised: {exc}")  # not an orphaned executing row
        duration = int((self.clock.now() - started).total_seconds() * 1000)
        success = result.ok and verify.success

        # Redact the whole observed payload — every effectful tool's output/stderr flows through
        # here into the durable record AND back to the model (fix: asymmetric redaction).
        await conn.execute(
            "UPDATE tool_execution SET status=$1, observed=$2, verification=$3, success=$4, "
            "  finished_at=now(), duration_ms=$5 WHERE id=$6",
            "verified_success" if success else "verified_failure",
            redact_obj({"output": result.output, "display": result.display, "error": result.error}),
            {"success": verify.success, "detail": verify.detail}, success, duration, exec_id,
        )
        await journal.set_state(RunState.UPDATE_STATE)
        await journal.event(
            "tool",
            {"name": tool.name, "ok": result.ok, "verified": verify.success, "success": success},
            latency_ms=duration,
        )
        await self._audit(
            conn, journal.run_id, tool, call.arguments, decision, result.ok, verify.success, duration
        )
        await self._emit(
            conn, "tool.completed" if success else "tool.failed", journal.run_id,
            {"tool": tool.name, "verified": verify.success, "success": success},
        )

        # Provenance (§10): a tool result IS a live observation made just now — tag it with source +
        # timestamp so the model treats it as current ground truth, never confuses it with a memory, and
        # can prefer it over a stale recall. (ssh_run additionally carries the remote host_id in output.)
        now_iso = self.clock.now().isoformat()
        if success:
            return (
                _tool_message(tool.name, {"ok": True, "source": "live_observation", "observed_at": now_iso,
                                          "output": redact_obj(result.output), "verified": True}),
                True, result.display or tool.name,
            )
        return (
            _tool_message(tool.name, {"ok": False, "source": "live_observation", "observed_at": now_iso,
                                      "error": redact_obj(result.error or verify.detail)}),
            False, result.error or verify.detail or result.display or "failed",
        )

    async def _audit(
        self,
        conn: Any,
        run_id: UUID,
        tool: Any,
        args: dict[str, Any],
        decision: Any,
        ok: bool | None,
        verified: bool | None,
        duration_ms: int | None,
    ) -> None:
        await conn.execute(
            "INSERT INTO tool_audit "
            "  (run_id, tool_name, risk_level, args_redacted, decision, ok, verified, duration_ms) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            run_id, tool.name, int(decision.risk), redact_obj(args), decision.action.value,
            ok, verified, duration_ms,
        )

    async def _system_critical_binaries(self) -> frozenset[str]:
        """The binaries classified SYSTEM_CRITICAL (§34), cached with a short TTL. On any DB hiccup,
        keep the last-known set (or empty) — the enforcement must never itself break a turn."""
        now = self.clock.now().timestamp()
        if self._syscrit is not None and (now - self._syscrit[1]) < _AUTHORITY_TTL:
            return self._syscrit[0]
        names: frozenset[str] = self._syscrit[0] if self._syscrit else frozenset()
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT t.name FROM tool_authority a JOIN discovered_tool t ON t.id=a.tool_id "
                    "WHERE a.valid_until IS NULL AND a.authority='system_critical' AND t.available")
            names = frozenset(r["name"] for r in rows)
        except Exception as exc:  # noqa: BLE001 - enforcement is best-effort over a cache
            self.log.warning("authority_floor_load_failed", error=str(exc))
        self._syscrit = (names, now)
        return names

    async def _authority_floor(
        self, tool: Any, args: dict[str, Any], decision: PolicyDecision
    ) -> PolicyDecision:
        """Enforce the discovered-tool authority record (§34/§35/§88): a command whose binary is
        classified SYSTEM_CRITICAL pauses to confirm, even if the tool's own risk assessment would
        auto-allow it. Model-external and immutable — the classification comes from the datastore, not
        from anything the model said. Complements exec_tool's command-level destructive check."""
        if decision.action is not Action.AUTO_ALLOW:
            return decision  # already gated or denied — nothing to tighten
        binary = _command_binary(tool.name, args)
        if binary and binary in await self._system_critical_binaries():
            return PolicyDecision(
                Action.CONFIRM,
                f"'{binary}' is classified system-critical — pausing to confirm first",
                RiskLevel.R4)
        return decision

    async def recover(self) -> list[dict[str, str]]:
        """Resolve runs left 'running' by a crash and, where safe, actually CONTINUE them (§9):
          • REPLAY_SAFE (no side effect was in flight) → re-drive the turn.
          • VERIFY_THEN_CONTINUE (a non-idempotent effect was in flight) → independently verify whether
            it landed (probe reality), record that truth on the tool row, then re-drive so Sali carries
            on from a KNOWN state instead of blindly repeating or aborting.
          • ABORT_SURFACE (unknowable) → surface, never guess.
        The old run is always superseded (aborted) and the resume is a fresh, journaled turn. Gated by
        settings.runtime.resume_interrupted and capped by resume_max_runs; stale-guarded to 2 minutes so
        a live run in another process is never touched."""
        resolved: list[dict[str, str]] = []
        to_redrive: list[tuple[Any, str, dict[str, str]]] = []
        async with self.pool.acquire() as conn:
            runs = await conn.fetch(
                "SELECT run_id, session_id, user_input, state FROM agent_runs "
                "WHERE status='running' AND updated_at < now() - interval '2 minutes'")
            for row in runs:
                state = RunState(row["state"])
                idempotent: bool | None = None
                interrupted = None
                if state is RunState.EXECUTE_TOOL:
                    interrupted = await conn.fetchrow(
                        "SELECT id, tool_name, plan FROM tool_execution "
                        "WHERE run_id=$1 AND status='executing' ORDER BY started_at DESC LIMIT 1",
                        row["run_id"],
                    )
                    if interrupted is not None:
                        tool = self.registry.get(interrupted["tool_name"])
                        idempotent = tool.idempotent if tool is not None else None
                # Atomically CLAIM this stale run before ANY recovery work: only the process whose
                # conditional UPDATE affects a row proceeds. A concurrently-starting process (a restart, or
                # terminal + API server booting together) loses the race (0 rows) and skips it — so a
                # still-live or interrupted run is never aborted+re-driven twice (Final audit §34).
                claimed = await conn.fetchval(
                    "UPDATE agent_runs SET status='aborted', state=$1, updated_at=now() "
                    "WHERE run_id=$2 AND status='running' RETURNING run_id",
                    RunState.ABORTED.value, row["run_id"])
                if claimed is None:
                    continue
                action = resume_action(state, idempotent)
                # §19: for a non-idempotent effect, re-observe reality to learn whether it landed.
                verified: VerifyResult | None = None
                if action is ResumeAction.VERIFY_THEN_CONTINUE and interrupted is not None:
                    verified = await self._verify_interrupted(conn, interrupted)
                payload: dict[str, Any] = {"action": action.value, "was_state": state.value}
                if verified is not None:
                    payload["verified"] = verified.success
                await conn.execute(
                    "INSERT INTO run_events (run_id, seq, kind, payload) VALUES "
                    "($1, (SELECT coalesce(max(seq),0)+1 FROM run_events WHERE run_id=$1), "
                    "'resumed', $2)",
                    row["run_id"], payload,
                )
                # (the run was already atomically aborted by the CLAIM above)
                out: dict[str, str] = {
                    "run_id": str(row["run_id"]), "was_state": state.value, "action": action.value
                }
                if verified is not None:
                    out["verified"] = str(verified.success)
                resolved.append(out)
                redrivable = action is ResumeAction.REPLAY_SAFE or (
                    action is ResumeAction.VERIFY_THEN_CONTINUE and verified is not None
                )
                if redrivable and row["user_input"] and row["session_id"]:
                    to_redrive.append((row["session_id"], row["user_input"], out))

        # Re-drive OUTSIDE the recovery connection (each turn takes its own), bounded + best-effort.
        # A re-drive is a CONTINUATION of the same logical work as a fresh, journaled run (new run_id,
        # same session) — the task_id it carries is unchanged; a context reset never forks the task.
        if self.settings.runtime.resume_interrupted:
            for session_id, user_input, out in to_redrive[: self.settings.runtime.resume_max_runs]:
                await self._emit_runtime(
                    "runtime.continuation_started",
                    {"was_run": out.get("run_id"), "was_state": out.get("was_state"),
                     "continuation": True},
                    session_id=session_id)
                try:
                    result = await self.run(user_input, session_id=session_id)
                    out["resumed_run"] = str(result.run_id)
                    await self._emit_runtime(
                        "runtime.continuation_completed",
                        {"was_run": out.get("run_id"), "resumed_run": str(result.run_id)},
                        run_id=result.run_id, session_id=session_id)
                except Exception as exc:  # noqa: BLE001 - a failed resume must not crash startup recovery
                    self.log.warning("recover re-drive failed: %s", exc)
        return resolved

    async def recover_tasks(self) -> list[dict[str, Any]]:
        """Detect and recover orphaned tasks after a crash or restart.

        Finds tasks with stale heartbeats (>2 min), marks them interrupted,
        and builds recovery context so they can be resumed.
        """
        from sali.tasks.recovery import detect_orphaned_tasks, mark_task_interrupted, recover_task

        orphaned = await detect_orphaned_tasks(self.pool)
        if not orphaned:
            return []

        await self._emit_runtime("runtime.recovery_started", {"orphaned": len(orphaned)})
        recovered = []
        for info in orphaned:
            task_id = UUID(info["task_id"])
            if not info["can_resume"]:
                # Exhausted retries — mark as failed
                await self._tasks.finish(task_id, status="failed",
                                         result=f"exhausted {info['max_retries']} retries")
                self.log.warning("task_exhausted_retries", task_id=info["task_id"])
                continue

            # Mark as interrupted (preserves primary status)
            await mark_task_interrupted(self.pool, task_id, reason="process_restart")
            with contextlib.suppress(Exception):
                append_event(task_id, "task_interrupted", {"reason": "process_restart"})

            # Build recovery context
            recovery = await recover_task(self.pool, task_id)
            recovered.append(recovery)
            with contextlib.suppress(Exception):
                append_event(task_id, "task_recovered",
                             {"retry_count": recovery.get("retry_count"),
                              "completed_steps": recovery.get("completed_steps")})
            # Deterministic, DB-driven continuation context is now ready for this task (§13) — the next
            # live turn resumes it from durable state via _open_tasks_note, never by asking the model
            # "what were we doing?". Announce it so clients see the recovery land.
            await self._emit_runtime(
                "runtime.recovery_completed",
                {"completed_steps": recovery.get("completed_steps"),
                 "total_steps": recovery.get("total_steps"),
                 "retry_count": recovery.get("retry_count")},
                task_id=task_id)
            self.log.debug("task_recovered", task_id=info["task_id"],
                          objective=info["objective"], retry=recovery.get("retry_count"))

        return recovered

    async def _verify_interrupted(self, conn: Any, exec_row: Any) -> VerifyResult | None:
        """Independently re-observe whether an interrupted non-idempotent tool's effect landed (§9/§19),
        and record that truth on the durable tool_execution row. Currently probes execute_command via the
        shell-effect probes; returns None (→ surface, don't re-drive) for effects we can't check."""
        if exec_row["tool_name"] != "execute_command":
            return None
        args = (exec_row["plan"] or {}).get("args") or {}
        command = args.get("command")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        if not isinstance(command, str) or not command.strip():
            return None
        verdict = await verify_effect(command)
        if verdict is None:
            return None
        await conn.execute(
            "UPDATE tool_execution SET status=$1, verification=$2, success=$3, finished_at=now() "
            "WHERE id=$4",
            "verified_success" if verdict.success else "verified_failure",
            {"success": verdict.success, "detail": verdict.detail, "recovered": True},
            verdict.success, exec_row["id"],
        )
        return verdict


    async def _ensure_conversation(self, conn: Any, session_id: UUID) -> None:
        inserted = await conn.fetchval(
            "INSERT INTO conversation (id, channel) VALUES ($1, 'terminal') "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            session_id,
        )
        if inserted is not None:
            await self._emit(
                conn, "conversation.created", session_id, {}, subject_type="conversation"
            )

    async def _append_message(
        self, conn: Any, session_id: UUID, role: str, content: str, *, model: str | None = None
    ) -> None:
        seq = await conn.fetchval(
            "SELECT coalesce(max(seq),0)+1 FROM message WHERE conversation_id=$1", session_id
        )
        await conn.execute(
            "INSERT INTO message (conversation_id, seq, role, content, model, token_count) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            session_id, seq, role, content, model, self.provider.count_tokens(content),
        )
        await conn.execute("UPDATE conversation SET last_active=now() WHERE id=$1", session_id)

    async def _load_history(self, conn: Any, session_id: UUID) -> list[tuple[str, str]]:
        """Prior turns: the running summary (if any) plus the recent verbatim messages."""
        conv = await conn.fetchrow(
            "SELECT summary, summary_through_seq, summary_packet FROM conversation WHERE id=$1", session_id
        )
        summary = conv["summary"] if conv else None
        through = conv["summary_through_seq"] if conv else 0
        rows = await conn.fetch(
            "SELECT role, content FROM message WHERE conversation_id=$1 AND seq > $2 "
            "ORDER BY seq DESC LIMIT 10",
            session_id, through,
        )
        history: list[tuple[str, str]] = [(r["role"], r["content"]) for r in reversed(rows)]
        # Prefer the structured packet (its task state is deterministic, §11-12); fall back to prose.
        packet = conv["summary_packet"] if conv else None
        earlier = (continuation.render_packet(packet) if packet else "") or summary
        if earlier:
            history.insert(0, ("earlier", earlier))
        return history

    def _context_window(self) -> int:
        """The model's effective context window — from the provider when it reports one, else the
        configured ctx_default (§28). Never assumed unbounded."""
        return context_budget.resolve_limit(self.provider, self.settings)

    def _budget(self, messages: list[ChatMessage]) -> context_budget.ContextBudget:
        """A deterministic snapshot of how full the working prompt is (§7) — drives fold + events."""
        return context_budget.budget_for(self.provider, self.settings, messages)

    def _over_context(self, messages: list[ChatMessage]) -> bool:
        """True when the working prompt is close enough to the model's window that we should fold it
        before the next call. Conservative (over-estimates) so we fold early, never overflow (§7)."""
        if len(messages) <= 3:
            return False  # system + first turn + one more — nothing worth folding yet
        return self._budget(messages).should_compact

    async def _compact_and_continue(
        self, messages: list[ChatMessage], journal: RunJournal, *,
        count: int, before: context_budget.ContextBudget,
        task_id: UUID | None, session_id: UUID | None, reason: str,
    ) -> list[ChatMessage]:
        """One safe compaction cycle: announce it, protect durable state, fold, and report the result.

        The invariant that makes 'compact and continue without losing a thing' true: everything that
        matters is ALREADY durable (task steps, executions, checkpoints, artifacts, progress) before we
        get here, and the deterministic task header in the fold is re-read from the store — so the fold
        can only lose disposable conversational context, never task state (§9). A compaction is recorded
        as task PROGRESS so the watchdog reads a slow fold as work, not as a stuck/orphaned task (§21)."""
        await self._emit_runtime(
            "runtime.context_compaction_started",
            {"count": count, "reason": reason, **before.as_dict()},
            run_id=journal.run_id, task_id=task_id, session_id=session_id)
        if task_id is not None:
            # Keep the watchdog correct across a slow fold: heartbeat (alive) + progress (working).
            with contextlib.suppress(Exception):
                await self._tasks.heartbeat(task_id)
            with contextlib.suppress(Exception):
                await self._tasks.record_progress(task_id, "compaction")
            # A reproducible context checkpoint/manifest BEFORE folding (§21/§23/§25): source IDs +
            # token estimate, never the raw context — enough to reconstruct the working set on restart.
            with contextlib.suppress(Exception):
                await self._write_context_checkpoint(task_id, journal.run_id, reason, before.used)
        folded = await self._fold_messages(messages)
        after = self._budget(folded)
        await journal.event(
            "context_folded", {"kept": len(folded), "count": count, "reason": reason,
                               "before": before.used, "after": after.used})
        await self._emit_runtime(
            "runtime.context_compaction_completed",
            {"count": count, "reason": reason, "kept": len(folded),
             "used_before": before.used, "used_after": after.used, "limit": after.limit},
            run_id=journal.run_id, task_id=task_id, session_id=session_id)
        return folded

    async def _write_context_checkpoint(
        self, task_id: UUID, run_id: UUID | None, reason: str, token_estimate: int,
    ) -> None:
        """Persist a reproducible context manifest (§23/§25): the durable source IDs the capsule drew on
        + a token estimate + the capsule version — NOT the raw context. Enough to reconstruct the working
        set after a restart. Best-effort; never breaks a compaction."""
        from sali.runtime.capsule import CONTEXT_VERSION, build_capsule

        task = await self._tasks.get(task_id)
        if task is None:
            return
        cap = await build_capsule(
            self.pool, task, reviewer=self._reviewer, research=self._research_store,
            skills=self._skills, decisions=self._decisions, phases=self._phases)
        cid: UUID | None = None
        async with self.pool.acquire() as conn:
            cid = await conn.fetchval(
                "INSERT INTO context_checkpoint "
                "  (task_id, run_id, step_seq, workspace, context_version, source_ids, token_estimate, reason) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                task_id, run_id, cap.next_step["seq"] if cap.next_step else None,
                cap.workspace_root, CONTEXT_VERSION, cap.source_ids(), token_estimate, reason)
        await self._emit_runtime(
            "context.checkpoint_created",
            {"checkpoint_id": str(cid) if cid else None, "context_version": CONTEXT_VERSION,
             "token_estimate": token_estimate, "reason": reason},
            run_id=run_id, task_id=task_id)

    async def _fold_messages(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Fold the middle of the working prompt (everything after the system + first user turn) into a
        compact carry-forward, so Sali continues with a small context. Prompt 6: when a task is active
        the carry-forward is the DETERMINISTIC Task State Capsule regenerated from durable storage — so
        operational state is lossless across the fold (no LLM summary, cheaper, reproducible §9/§10/§49).
        Only pure conversation (no task) falls back to a model-written progress note."""
        system, first_user = messages[0], messages[1]
        body = messages[2:]
        task = await self._current_task_safe()
        if task is not None:
            body_note = await self._task_capsule_note(task)  # lossless operational state, from Postgres
        else:
            transcript = "\n".join(f"{m.role}: {(m.content or '')[:2000]}" for m in body)
            note = await self.provider.chat(
                [ChatMessage(role="system", content=continuation.PACKET_INSTRUCTION),
                 ChatMessage(role="user", content=transcript)],
                options=_SUMMARIZE,
            )
            body_note = note.content.strip()
        folded = [
            system, first_user,
            ChatMessage(
                role="user",
                content=f"(Where you are in this task so far — continue from here:\n{body_note})",
            ),
        ]
        # Keep the single most recent concrete result verbatim (as plain context, so there are no
        # orphaned tool-call pairings) — so Sali doesn't redo the step it just finished.
        recent = next((m for m in reversed(body) if (m.content or "").strip()), None)
        if recent is not None:
            folded.append(ChatMessage(
                role="user", content=f"(Your most recent result, in full:\n{(recent.content or '')[:6000]})"
            ))
        return folded

    def _maybe_learn(self) -> None:
        """Fire a throttled, OFF-THREAD consolidation pass (§17-19) so memory actually ACCRUES during
        normal use: mine procedures, record failures→fixes, distill an episode. Fire-and-forget so it
        never adds turn latency; skipped if one is already running or ran within _CONSOLIDATE_EVERY_S.
        Without this the six memory types never come into existence during a normal session."""
        if self.learning is None:
            return
        if self._consolidating is not None and not self._consolidating.done():
            return  # one already in flight — don't stack distillations
        now = self.clock.now()
        if (self._last_consolidate is not None
                and (now - self._last_consolidate).total_seconds() < _CONSOLIDATE_EVERY_S):
            return  # ran recently — throttle
        self._last_consolidate = now
        self._consolidating = asyncio.create_task(self._consolidate_safe())

    async def _consolidate_safe(self) -> None:
        """Run one consolidation pass, swallowing any error — background learning must never crash
        the session, and a distillation hiccup just means we try again next window."""
        try:
            await self.learning.consolidate()
        except Exception as exc:  # noqa: BLE001 - never let background learning surface as a crash
            self.log.warning("consolidate_failed", error=str(exc))

    async def _maybe_compact(self, conn: Any, session_id: UUID) -> None:
        """When the conversation grows long, summarize older turns so the session never breaks."""
        row = await conn.fetchrow(
            "SELECT summary, summary_through_seq, "
            "  (SELECT max(seq) FROM message WHERE conversation_id=$1) AS max_seq, "
            "  (SELECT count(*) FROM message WHERE conversation_id=$1 "
            "     AND seq > summary_through_seq) AS uncompacted "
            "FROM conversation WHERE id=$1",
            session_id,
        )
        if row is None or (row["uncompacted"] or 0) < _COMPACT_AFTER:
            return
        cutoff = int(row["max_seq"]) - _KEEP_RECENT
        if cutoff <= int(row["summary_through_seq"]):
            return
        older = await conn.fetch(
            "SELECT role, content FROM message WHERE conversation_id=$1 AND seq > $2 AND seq <= $3 "
            "ORDER BY seq",
            session_id, row["summary_through_seq"], cutoff,
        )
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in older)
        prompt = [
            ChatMessage(role="system", content=continuation.PACKET_INSTRUCTION),
            ChatMessage(role="user", content=f"Prior summary:\n{row['summary'] or '(none)'}\n\n"
                                             f"Earlier turns:\n{transcript}"),
        ]
        result = await self.provider.chat(prompt, options=_SUMMARIZE)
        summary_text = result.content.strip()
        # §11-12: store the prose AND a machine-readable packet whose task state is deterministic.
        packet = continuation.build_packet(await self._current_task_safe(), summary_text)
        await conn.execute(
            "UPDATE conversation SET summary=$1, summary_through_seq=$2, summary_packet=$3 WHERE id=$4",
            summary_text, cutoff, packet, session_id,
        )
        await self._emit(conn, "conversation.compacted", session_id,
                         {"through_seq": cutoff}, subject_type="conversation")

    async def _emit_runtime(
        self, event_type: str, data: dict[str, Any], *,
        run_id: UUID | None = None, task_id: UUID | None = None, session_id: UUID | None = None,
    ) -> None:
        """Emit a canonical runtime.* event (context compaction / continuation / recovery) so every
        client — terminal and iPhone — can observe 'Sali is compacting…' then 'Sali continued' (§22).
        Carries task/run/session identity; only operational counts, never prompt content or reasoning.
        Best-effort: observability must never break a turn or a recovery."""
        if self._publisher is None:
            return
        from sali.events.publisher import SaliEvent
        with contextlib.suppress(Exception):
            await self._publisher.publish(SaliEvent(
                event_type=event_type, run_id=run_id, task_id=task_id, session_id=session_id,
                origin="runtime", data=data))

    async def _emit(
        self,
        conn: Any,
        event_type: str,
        subject_id: UUID,
        payload: dict[str, Any],
        *,
        subject_type: str = "agent_run",
    ) -> None:
        """Emit a durable action event via the canonical publisher."""
        if self._publisher is not None:
            from sali.events.publisher import SaliEvent
            with contextlib.suppress(Exception):
                await self._publisher.publish(SaliEvent(
                    event_type=event_type,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    origin="agent",
                    data=payload,
                ))
        else:
            # Fallback: direct SQL (backward compatibility)
            await conn.execute(
                "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                "VALUES ($1,$2,$3,$4)",
                event_type, subject_type, subject_id, payload,
            )


# No single tool result may swallow the whole context window, but Sali is powerful and should SEE
# most of a file/command/ssh output — with 65K context, give it a very generous slice so it can
# reason over full file contents and command outputs. The fold guard still prevents overflow,
# and the full result always lives in the durable record.
_TOOL_OUTPUT_CAP = 48_000


def _tool_message(tool_name: str, payload: dict[str, Any]) -> ChatMessage:
    content = json.dumps(payload)
    if len(content) > _TOOL_OUTPUT_CAP:
        content = content[:_TOOL_OUTPUT_CAP] + f"… [truncated; {len(content)} bytes total]"
    return ChatMessage(role="tool", name=tool_name, content=content)


def _circuit_broken_message(tool_name: str, failures: int, error: str) -> ChatMessage:
    """A factual tool result (not a coaching paragraph) saying this exact call keeps failing and was
    not run again — enough for the model to try something different."""
    return _tool_message(
        tool_name, {"ok": False, "refused": True, "failures": failures, "error": error}
    )
