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
from sali.core.enums import MemoryLayer, MemorySource
from sali.core.errors import ProviderError
from sali.core.ids import new_id
from sali.core.scope import project_scope
from sali.graph.service import GraphService
from sali.ingest.service import IngestService
from sali.learning.episodes import prune_stm
from sali.memory.writer import observe
from sali.obs.log import get_logger
from sali.perception.service import build_perception
from sali.provider.base import ChatMessage, ChatResult, ModelProvider, ToolCall
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.runtime.journal import RunJournal
from sali.runtime.state import RunState, resume_action
from sali.scheduler.store import ScheduleStore
from sali.security.confirm import Confirmer
from sali.security.policy import Action, PolicyEngine, SessionGrants
from sali.security.redact import redact_obj
from sali.tasks.store import TaskStore
from sali.tools import dispatch
from sali.tools.base import VerifyResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry
from sali.tools.remote import build_remote_runner
from sali.twin import awareness as twin_awareness

# Warmer sampling so Sali sounds like a person, not a deterministic tool. Tool-calling is
# rendered structurally by the model, so it still works reliably at this temperature.
_VOICE: dict[str, Any] = {
    "temperature": 0.7, "top_k": 40, "top_p": 0.95, "presence_penalty": 0.3, "repeat_penalty": 1.1,
}
_SUMMARIZE: dict[str, Any] = {"temperature": 0.3, "top_k": 40, "top_p": 0.9}

# Fold older turns into a running summary once this many uncompacted messages accumulate,
# always keeping the most recent few verbatim.
_COMPACT_AFTER = 24
_KEEP_RECENT = 6

# Intra-turn context folding: when the *working* prompt for a single task nears the model's
# window, fold everything done so far into a compact progress note and carry on — so a long,
# many-step job continues on its own instead of dead-ending on the context limit.
_CTX_HEADROOM = 0.78  # use more of the window before folding (the fold still prevents overflow)
_CTX_MARGIN = 8       # per-message token overhead added to the estimate
_FOLD_INSTRUCTION = (
    "You are Sali, in the middle of a task. Fold everything you've done so far on THIS task into "
    "a tight progress note you can pick up from: what Almir asked for, what you've tried, what the "
    "tools returned, what you've concluded, and exactly what's still left to do. Be explicit about "
    "steps you've ALREADY completed so you don't repeat them. First person, concrete, compact — it "
    "replaces the detailed history so you can keep going. Just the note."
)

# Follow-through: models sometimes *narrate* an action ("I'll run these in parallel") and then
# stop without calling anything, leaving Almir to re-prompt. When a reply defers a machine
# action but emits no tool call, we nudge Sali to actually do it — bounded, so it can't loop.
# The model occasionally emits a malformed tool call the backend can't parse (a transient 500).
# Re-sampling at the warm voice temperature almost always yields a clean one, so retry a couple
# of times before giving up rather than crashing the whole turn.
_MAX_PROVIDER_RETRIES = 2

# Enough pushes to carry a multi-step build (a folder + several files) to completion, bounded so
# it can never spin.
_MAX_FOLLOW_THROUGH = 4
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
_MAX_TOOL_FAILURES = 2
# How often the background consolidation pass (§17-19: mine procedures, record failures, distill an
# episode) may run. Throttled so a busy session doesn't distill every turn; it runs OFF-THREAD after
# the turn is already done, so it never adds latency to Almir's reply.
_CONSOLIDATE_EVERY_S = 600.0

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


class _RecallSink:
    """Active memory recall for Sali's memory tools (§34,§55,§56): hybrid search, graph traversal, and
    history — every result carries provenance + confidence + a staleness flag so the model reasons
    over evidence and never has to invent a memory (§47). Read-only, over the retrieval + graph engines."""

    def __init__(self, memory: Any, graph: Any) -> None:  # MemoryService, GraphService
        self._memory = memory
        self._graph = graph

    async def search(self, query: str, *, layer: str | None = None, k: int = 6) -> list[dict[str, Any]]:
        # For a layer-filtered read, pull a wider slate then keep the top-k of that layer.
        hits = await self._memory.retrieve(query, k=k if layer is None else max(k * 4, 24))
        if layer is not None:
            hits = [h for h in hits if h.memory.layer.value == layer]
        out: list[dict[str, Any]] = []
        for h in hits[:k]:
            m = h.memory
            entry: dict[str, Any] = {
                "content": m.content, "layer": m.layer.value, "source": m.source.value,
                "confidence": round(h.effective_confidence, 2), "stale": h.stale,
                "recorded": m.valid_from.date().isoformat() if m.valid_from else None,
            }
            if m.structured:  # a structured incident/experience/procedure — give Sali the whole shape
                entry["detail"] = m.structured
            out.append(entry)
        return out

    async def related(self, entity: str, *, hops: int = 1) -> dict[str, Any]:
        nodes = await self._graph.find_by_name(entity, limit=3)
        if not nodes:
            return {"entity": entity, "found": False, "relations": []}
        node = nodes[0]
        relations: list[dict[str, Any]] = []
        if hops <= 1:
            for hop in await self._graph.neighbors(node.id):
                relations.append({"relation": hop["rel_type"], "target": hop["node"].name,
                                  "type": hop["node"].node_type, "confidence": round(hop["confidence"], 2)})
        else:
            for r in await self._graph.traverse(node.id, max_depth=min(hops, 4)):
                if r["depth"] > 0:
                    relations.append({"target": r["name"], "type": r["node_type"], "hops": r["depth"]})
        return {"entity": entity, "found": True, "resolved": node.name, "type": node.node_type,
                "relations": relations[:24]}

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


@dataclass(slots=True)
class AgentResult:
    run_id: UUID
    text: str
    iterations: int
    tool_calls: int


@dataclass(slots=True)
class LoopEvent:
    """A streamed moment of a turn, for live rendering (terminal or WebSocket)."""

    kind: str  # 'status' | 'token' | 'thinking' | 'tool' | 'final' | 'error'
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
        self._memory_sink = _MemorySink(retrieval.memory)  # lets the remember tool save durably
        self._graph = GraphService(pool)  # lets the relate tool write conversational edges
        self._recall = _RecallSink(retrieval.memory, self._graph)  # lets memory tools actively query (§34)
        self._tasks = TaskStore(pool)  # persistent multi-step tasks (§24), resumed across restarts
        self._schedules = ScheduleStore(pool, self.clock)  # recurring work (§44)
        self._documents = IngestService(pool, provider)  # document ingestion → memory (§44)
        self._remote = build_remote_runner(settings.ssh, SecretStore())  # ssh; vault-backed passwords
        self._comms = CommsService(settings.comms, SecretStore())  # email + calendar (§44)
        self._browser = build_browser(settings.browser)  # Sali's own Firefox (§44); launched lazily
        self._vision = _VisionSink(provider)  # look at the screen locally (sali3 §33-35)
        self._perception = build_perception(settings)  # focused app/window + a11y tree (sali3 §2,8,9)
        self._consolidating: asyncio.Task[Any] | None = None
        self._last_consolidate: Any = None

    async def aclose(self) -> None:
        """Release loop-lifetime resources (Sali's live browser). Best-effort; safe to call twice."""
        with contextlib.suppress(Exception):
            await self._browser.aclose()

    async def run(self, user_input: str, session_id: UUID | None = None) -> AgentResult:
        """Run one turn to completion (non-streaming) by consuming the event stream."""
        final: LoopEvent | None = None
        async for event in self.astream(user_input, session_id):
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
        """A compact view of any task still in progress (§24), so Sali resumes it — this is read
        from the datastore every turn, which is exactly what makes a task survive a restart.
        Best-effort: the task engine is a nicety, never a reason to break a turn."""
        try:
            tasks = await self._tasks.open_tasks(limit=3)
        except Exception:  # noqa: BLE001 - tasks are a nicety, never break a turn
            return None
        if not tasks:
            return None
        lines = "\n".join(f"- {t.one_line()}" for t in tasks)
        return (
            "Task(s) you have in progress — pick up where you left off, advancing steps as you go:\n"
            + lines
        )

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
        self, user_input: str, session_id: UUID | None = None
    ) -> AsyncIterator[LoopEvent]:
        """Drive one turn, streaming status/token/thinking/tool events as they happen. This is
        the real core; ``run`` is a thin consumer. Everything is journaled exactly as before."""
        session_id = session_id or new_id()
        grants = SessionGrants()
        async with self.pool.acquire() as conn:  # journal + tool + persistence connection
            await self._ensure_conversation(conn, session_id)
            history = await self._load_history(conn, session_id)
            await self._append_message(conn, session_id, "user", user_input)
            journal = await RunJournal.start(conn, session_id, user_input)
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
                     "tools": len(bundle.tool_facts), "needs_live": plan.needs_live},
                )

                await journal.set_state(RunState.BUILD_CONTEXT)
                specs = self.registry.advertise()
                # The 'interpret' branch of observation (§16): notice machine changes that
                # happened while Almir was away, so Sali can bring them up in its own words.
                machine_changes, ack_changes_through, ack_obs_through = await self._machine_changes(
                    conn, journal)
                tasks_note = await self._open_tasks_note()  # §24: resume any task in progress
                assembled = self.context.assemble(
                    user_input, bundle, specs,
                    live_note=LIVE_NOTE if plan.needs_live else None, history=history,
                    machine_changes=machine_changes, tasks_note=tasks_note,
                )
                messages = list(assembled.messages)
                await journal.event(
                    "context",
                    {"included": assembled.included, "dropped": assembled.dropped,
                     "est_tokens": assembled.est_tokens, "conflicts": len(assembled.conflicts)},
                )

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

                while iteration < max_iter and tokens_used < budget:
                    await journal.set_state(RunState.REASON_PLAN, iteration=iteration)
                    # Nearing the window on a long task? Fold what's done and keep going — the
                    # task never dead-ends on the context limit; it compacts and continues.
                    if self._over_context(messages):
                        yield LoopEvent("status", "compacting to keep going")
                        messages = await self._fold_messages(messages)
                        await journal.event("context_folded", {"kept": len(messages)})
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
                        tool_msg, ok, summary = await self._handle_tool(conn, journal, grants, call)
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
                raise

    async def _handle_tool(
        self, conn: Any, journal: RunJournal, grants: SessionGrants, call: ToolCall
    ) -> tuple[ChatMessage, bool, str]:
        """Run one tool; return (message-for-the-model, succeeded?, short summary) — the flag +
        summary let the UI print a clean ✓/✗ progress line of what was actually done."""
        tool = self.registry.get(call.name)
        if tool is None:
            await journal.event("tool.unknown", {"name": call.name})
            return _tool_message(call.name, {"error": f"unknown tool '{call.name}'"}), False, "unknown tool"

        decision = self.policy.decide(tool, call.arguments, grants)
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

        # Write-ahead: the 'executing' row is committed BEFORE the side effect (fix M15).
        exec_id = new_id()
        await conn.execute(
            "INSERT INTO tool_execution "
            "  (id, run_id, run_kind, tool_name, status, danger_level, approved_by, plan) "
            "VALUES ($1,$2,'agent_run',$3,'executing',$4,$5,$6)",
            exec_id, journal.run_id, tool.name, int(decision.risk),
            f"policy:{decision.action.value}", {"args": redact_obj(call.arguments)},
        )
        await journal.set_state(RunState.EXECUTE_TOOL)
        started = self.clock.now()
        ctx = ToolContext(settings=self.settings, clock=self.clock, pool=self.pool,
                          memory=self._memory_sink, recall=self._recall, graph=self._graph,
                          tasks=self._tasks,
                          schedules=self._schedules, documents=self._documents, remote=self._remote,
                          comms=self._comms, browser=self._browser, vision=self._vision,
                          perception=self._perception)
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

        if success:
            return (
                _tool_message(tool.name, {"ok": True, "output": redact_obj(result.output),
                                          "verified": True}),
                True, result.display or tool.name,
            )
        return (
            _tool_message(tool.name, {"ok": False, "error": redact_obj(result.error or verify.detail)}),
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

    async def recover(self) -> list[dict[str, str]]:
        """Resolve runs left 'running' by a crash. Phase 1 surfaces them (never silently
        retries); the correct resume action is computed and journaled for each."""
        resolved: list[dict[str, str]] = []
        async with self.pool.acquire() as conn:
            # Only STALE runs — a live run in another process (the daemon, a second `sali agent`, a
            # WebSocket turn) bumps updated_at on every FSM transition, so a fresh timestamp means it's
            # still in flight. Without this, one process starting up would abort another's active run.
            runs = await conn.fetch(
                "SELECT run_id, state FROM agent_runs "
                "WHERE status='running' AND updated_at < now() - interval '2 minutes'")
            for row in runs:
                state = RunState(row["state"])
                idempotent: bool | None = None
                if state is RunState.EXECUTE_TOOL:
                    exec_row = await conn.fetchrow(
                        "SELECT tool_name FROM tool_execution "
                        "WHERE run_id=$1 AND status='executing' ORDER BY started_at DESC LIMIT 1",
                        row["run_id"],
                    )
                    if exec_row is not None:
                        tool = self.registry.get(exec_row["tool_name"])
                        idempotent = tool.idempotent if tool is not None else None
                action = resume_action(state, idempotent)
                await conn.execute(
                    "INSERT INTO run_events (run_id, seq, kind, payload) VALUES "
                    "($1, (SELECT coalesce(max(seq),0)+1 FROM run_events WHERE run_id=$1), "
                    "'resumed', $2)",
                    row["run_id"], {"action": action.value, "was_state": state.value},
                )
                # Phase 1 always surfaces (never silently retries); the computed resume action
                # is journaled above for a future phase to act on.
                await conn.execute(
                    "UPDATE agent_runs SET status='aborted', state=$1, updated_at=now() "
                    "WHERE run_id=$2",
                    RunState.ABORTED.value, row["run_id"],
                )
                resolved.append(
                    {"run_id": str(row["run_id"]), "was_state": state.value, "action": action.value}
                )
        return resolved


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
            "SELECT summary, summary_through_seq FROM conversation WHERE id=$1", session_id
        )
        summary = conv["summary"] if conv else None
        through = conv["summary_through_seq"] if conv else 0
        rows = await conn.fetch(
            "SELECT role, content FROM message WHERE conversation_id=$1 AND seq > $2 "
            "ORDER BY seq DESC LIMIT 8",
            session_id, through,
        )
        history: list[tuple[str, str]] = [(r["role"], r["content"]) for r in reversed(rows)]
        if summary:
            history.insert(0, ("earlier", summary))
        return history

    def _context_window(self) -> int:
        return min(self.settings.model.ctx_default, self.settings.model.ctx_max)

    def _over_context(self, messages: list[ChatMessage]) -> bool:
        """True when the working prompt is close enough to the model's window that we should fold
        it before the next call. Conservative (over-estimates) so we fold early, never overflow."""
        if len(messages) <= 3:
            return False  # system + first turn + one more — nothing worth folding yet
        limit = int(self._context_window() * _CTX_HEADROOM)
        est = sum(
            int(self.provider.count_tokens(m.content or "") * 1.3) + _CTX_MARGIN for m in messages
        )
        return est > limit

    async def _fold_messages(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Fold the middle of the working prompt (everything after the system + first user turn)
        into one model-written progress note, so Sali continues the task with a small context.
        Model-driven, not scripted: the note is whatever Sali says it needs to carry forward."""
        system, first_user = messages[0], messages[1]
        body = messages[2:]
        transcript = "\n".join(f"{m.role}: {(m.content or '')[:2000]}" for m in body)
        note = await self.provider.chat(
            [ChatMessage(role="system", content=_FOLD_INSTRUCTION),
             ChatMessage(role="user", content=transcript)],
            options=_SUMMARIZE,
        )
        folded = [
            system, first_user,
            ChatMessage(
                role="user",
                content=f"(Where you are in this task so far — continue from here:\n{note.content.strip()})",
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
            result = await self.learning.consolidate()
            self.log.info("consolidated", procedures=len(result.procedures),
                          episodes=result.episodes_created, failures=result.failures_recorded,
                          pruned=result.stm_pruned)
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
            ChatMessage(role="system", content=(
                "You are Sali. Fold this earlier stretch of conversation into a tight running "
                "memory you'll carry forward — the facts about Almir, decisions made, tasks in "
                "progress, and where things stand. First person, compact, merge with the prior "
                "summary. Just the memory, nothing else."
            )),
            ChatMessage(role="user", content=f"Prior summary:\n{row['summary'] or '(none)'}\n\n"
                                             f"Earlier turns:\n{transcript}"),
        ]
        result = await self.provider.chat(prompt, options=_SUMMARIZE)
        await conn.execute(
            "UPDATE conversation SET summary=$1, summary_through_seq=$2 WHERE id=$3",
            result.content.strip(), cutoff, session_id,
        )
        await self._emit(conn, "conversation.compacted", session_id,
                         {"through_seq": cutoff}, subject_type="conversation")

    async def _emit(
        self,
        conn: Any,
        event_type: str,
        subject_id: UUID,
        payload: dict[str, Any],
        *,
        subject_type: str = "agent_run",
    ) -> None:
        """Emit a durable action event into the append-only `event` log (spec §25) — distinct
        from the per-run `run_events` crash-resume journal."""
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) VALUES ($1,$2,$3,$4)",
            event_type, subject_type, subject_id, payload,
        )


# No single tool result may swallow the whole context window, but Sali is powerful and should SEE
# most of a file/command/ssh output — 12 KB was cutting real reads. Give it a generous slice; the
# fold guard still prevents overflow, and the full result always lives in the durable record.
_TOOL_OUTPUT_CAP = 24_000


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
