"""The agent execution loop — the explicit FSM at Sali's core.

INPUT → RETRIEVE → BUILD_CONTEXT → REASON_PLAN → [AWAIT_CONFIRM → EXECUTE_TOOL → OBSERVE →
VERIFY → UPDATE_STATE]* → LEARN → RESPOND. The LLM is called at exactly one hot-loop site
(REASON_PLAN); retrieve, dispatch, verify, and journaling are deterministic. Every tool
effect is journaled ``executing`` *before* it runs (fix M15) and verified after — a tool is
never assumed to have succeeded (rule 13).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sali.config.settings import Settings
from sali.context.engine import LIVE_NOTE, ContextEngine
from sali.core.clock import Clock, SystemClock
from sali.core.enums import MemorySource
from sali.core.errors import ProviderError
from sali.core.ids import new_id
from sali.memory.writer import observe
from sali.obs.log import get_logger
from sali.provider.base import ChatMessage, ChatResult, ModelProvider, ToolCall
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.runtime.journal import RunJournal
from sali.runtime.state import RunState, resume_action
from sali.security.confirm import Confirmer
from sali.security.policy import Action, PolicyEngine, SessionGrants
from sali.security.redact import redact_obj
from sali.tools import dispatch
from sali.tools.base import VerifyResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry
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
_CTX_HEADROOM = 0.65  # fold once the estimated prompt passes this fraction of the window
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


def _looks_like_leaked_tool_call(text: str) -> bool:
    """A response whose content is really a tool-call JSON blob the parser didn't catch. We treat
    it as a malfunction to recover from, not as an answer to show."""
    stripped = text.strip()
    return stripped.startswith(("{", "[")) and bool(_LEAKED_TOOL_JSON.match(stripped))


# Stall detection is structural, not a phrase list: only a SHORT, tool-less first reply is even a
# candidate (a substantial answer is assumed complete and never checked), and then the reasoning
# engine judges its OWN reply — did it act, or only announce and stop? This is what the model is
# for (judgement, not parsing), and it can't drift out of date the way a verb list does.
_STALL_MAX_CHARS = 240
_STALL_JUDGE_SYSTEM = (
    "Almir asked you to do something and you just replied. Judge your OWN reply against the FULL "
    "task: did you actually finish everything he asked and give him the result — or did you only "
    "announce it, do PART of it, or say you'll 'continue' / 'keep building' / 'work on it' WITHOUT "
    "actually finishing in this reply? You have no background process, so if there's more to do, it "
    "is NOT done. A genuine question back to Almir, or a fully-finished task, counts as DONE. Reply "
    "with exactly one word: DONE or STALLED."
)


@dataclass(slots=True)
class AgentResult:
    run_id: UUID
    text: str
    iterations: int
    tool_calls: int


@dataclass(slots=True)
class LoopEvent:
    """A streamed moment of a turn, for live rendering (terminal or WebSocket)."""

    kind: str  # 'status' | 'token' | 'thinking' | 'tool' | 'reset' | 'final' | 'error'
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
    ) -> None:
        self.pool = pool
        self.provider = provider
        self.retrieval = retrieval
        self.context = context
        self.registry = registry
        self.policy = policy
        self.confirmer = confirmer
        self.settings = settings
        self.clock = clock or SystemClock()
        self.log = get_logger("sali.loop")

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

    async def _machine_changes(self, conn: Any, journal: RunJournal) -> tuple[str | None, int | None]:
        """A one-line heads-up about machine changes Sali hasn't noticed yet, plus the watermark
        to acknowledge — but NOT acknowledged here: the caller acks only once the turn actually
        reached the model, so a failed turn doesn't silently swallow the change."""
        try:
            phrases, through = await twin_awareness.unacknowledged_changes(conn)
        except Exception:  # noqa: BLE001 - awareness is a nicety, never break a turn
            return None, None
        if not phrases:
            return None, None
        await journal.event("twin_changes", {"count": len(phrases)})
        note = (
            "While Almir was away, some things changed on your machine: "
            + "; ".join(phrases[:6])
            + ". If it's worth a heads-up, mention it to him naturally and briefly, in your own "
            "words — don't make a big deal of it."
        )
        return note, through

    async def _stalled(self, user_input: str, response: str) -> bool:
        """Did Sali announce an action and stop, instead of doing it? Structural pre-filter (only a
        short, tool-less reply is a candidate — a substantial answer is taken as complete), then the
        model judges its own reply. No hardcoded phrases, so it never drifts out of date."""
        text = response.strip()
        if not text or len(text) > _STALL_MAX_CHARS:
            return False
        try:
            verdict = await self.provider.chat(
                [ChatMessage(role="system", content=_STALL_JUDGE_SYSTEM),
                 ChatMessage(role="user", content=f"Almir asked: {user_input}\n\nYour reply: {text}")],
                options=_SUMMARIZE,
            )
        except Exception:  # noqa: BLE001 - a failed judgement just means no nudge, never a broken turn
            return False
        return verdict.content.strip().upper().startswith("STALL")

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
                bundle = await self.retrieval.gather(user_input, plan, k=5)
                await journal.event(
                    "retrieve",
                    {"intent": plan.intent, "memories": len(bundle.memories),
                     "graph": len(bundle.graph_facts), "recent": len(bundle.recent),
                     "needs_live": plan.needs_live},
                )

                await journal.set_state(RunState.BUILD_CONTEXT)
                specs = self.registry.advertise()
                # The 'interpret' branch of observation (§16): notice machine changes that
                # happened while Almir was away, so Sali can bring them up in its own words.
                machine_changes, ack_changes_through = await self._machine_changes(conn, journal)
                assembled = self.context.assemble(
                    user_input, bundle, specs,
                    live_note=LIVE_NOTE if plan.needs_live else None, history=history,
                    machine_changes=machine_changes,
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
                json_recovery = 0

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
                        streamed = False
                        try:
                            async for chunk in self.provider.chat_stream(
                                messages, tools=specs or None, options=_VOICE
                            ):
                                if chunk.thinking:
                                    yield LoopEvent("thinking", chunk.thinking)
                                if chunk.content:
                                    streamed = True
                                    yield LoopEvent("token", chunk.content)
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
                            if streamed:
                                yield LoopEvent("reset")  # drop any partial before re-sampling
                            yield LoopEvent("status", "let me try that again")
                    latency = int((self.clock.now() - started).total_seconds() * 1000)
                    tokens_used += res.tokens_in + res.tokens_out
                    await journal.event(
                        "llm_call", {"model": res.model, "tool_calls": len(res.tool_calls)},
                        latency_ms=latency, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                    )
                    if not res.tool_calls:
                        # A tool call that leaked out as raw JSON text — never leave Almir staring
                        # at it. Wipe what streamed and ask for a clean redo (bounded).
                        if json_recovery < _MAX_JSON_RECOVERY and _looks_like_leaked_tool_call(res.content):
                            json_recovery += 1
                            yield LoopEvent("reset")
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            messages.append(ChatMessage(role="user", content=_JSON_RECOVERY_NUDGE))
                            await journal.event("json_recovery", {"attempt": json_recovery})
                            yield LoopEvent("status", "let me redo that")
                            iteration += 1
                            continue
                        # If Sali announced or half-did the task and stopped — even AFTER some tool
                        # calls (the "created the folder, now continuing…" stall) — push it to
                        # finish. Bounded so it can never spin; the model judges its own reply.
                        if (follow_through < _MAX_FOLLOW_THROUGH
                                and await self._stalled(user_input, res.content)):
                            follow_through += 1
                            yield LoopEvent("reset")  # clear the preamble; the real answer streams fresh
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            messages.append(ChatMessage(role="user", content=_FOLLOW_THROUGH_NUDGE))
                            await journal.event("follow_through", {"attempt": follow_through})
                            yield LoopEvent("status", "on it")
                            iteration += 1
                            continue
                        final_text = res.content
                        break
                    messages.append(
                        ChatMessage(role="assistant", content=res.content, tool_calls=res.tool_calls)
                    )
                    # Whatever Sali said before acting was thinking-out-loud, not the answer — wipe
                    # it from the display so only the final response remains, clean. The work itself
                    # shows as the animation (the tool events below), not as chat text.
                    yield LoopEvent("reset")
                    for call in res.tool_calls:
                        tool_calls += 1
                        yield LoopEvent("tool", call.name,
                                        {"phase": "start", "name": call.name, "args": call.arguments})
                        tool_msg = await self._handle_tool(conn, journal, grants, call)
                        yield LoopEvent("tool", call.name, {"phase": "done", "name": call.name})
                        messages.append(tool_msg)
                    iteration += 1
                else:
                    if not final_text:  # ran long — let Sali wrap up in its own voice, streamed
                        yield LoopEvent("status", "wrapping up")
                        messages.append(ChatMessage(
                            role="user",
                            content="(You've done plenty here — wrap up now in your own words, no more tools.)",
                        ))
                        acc = ""
                        async for chunk in self.provider.chat_stream(messages, options=_VOICE):
                            if chunk.content:
                                acc += chunk.content
                                yield LoopEvent("token", chunk.content)
                        final_text = acc.strip() or "Let me stop here for now."

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
                    await self._maybe_compact(conn, session_id)  # keep the one session from breaking
                except Exception as exc:  # noqa: BLE001 - housekeeping, never fail a done turn
                    self.log.warning("post_turn_housekeeping_failed", error=str(exc))
            except Exception as exc:
                self.log.error("run_failed", run_id=str(journal.run_id), error=str(exc))
                await journal.event("run.error", {"error": str(exc)})
                await journal.set_state(RunState.FAILED)
                await journal.finish("failed")
                raise

    async def _handle_tool(
        self, conn: Any, journal: RunJournal, grants: SessionGrants, call: ToolCall
    ) -> ChatMessage:
        tool = self.registry.get(call.name)
        if tool is None:
            await journal.event("tool.unknown", {"name": call.name})
            return _tool_message(call.name, {"error": f"unknown tool '{call.name}'"})

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
            return _tool_message(tool.name, {"denied": decision.reason})

        if decision.action is Action.CONFIRM:
            await journal.set_state(RunState.AWAIT_CONFIRM)
            if not await self.confirmer.confirm(tool, call.arguments, decision):
                await journal.set_state(RunState.TOOL_DENIED)
                await self._audit(
                    conn, journal.run_id, tool, call.arguments, decision, None, None, None
                )
                await self._emit(conn, "tool.denied", journal.run_id,
                                 {"tool": tool.name, "reason": "user declined"})
                return _tool_message(tool.name, {"denied": "user declined"})

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
        ctx = ToolContext(settings=self.settings, clock=self.clock, pool=self.pool)
        result = await dispatch.run_tool(tool, call.arguments, ctx)

        await journal.set_state(RunState.OBSERVE)
        await journal.set_state(RunState.VERIFY)
        try:
            verify = await tool.verify(call.arguments, result)
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
            return _tool_message(
                tool.name, {"ok": True, "output": redact_obj(result.output), "verified": True}
            )
        return _tool_message(
            tool.name, {"ok": False, "error": redact_obj(result.error or verify.detail)}
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
            runs = await conn.fetch("SELECT run_id, state FROM agent_runs WHERE status='running'")
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


# No single tool result may swallow the whole context window: a read/command can be 64 KiB
# (~the entire window), which would crowd out everything else and confuse the model into
# re-fetching. Bound what feeds back to it; the full result still lives in the durable record.
_TOOL_OUTPUT_CAP = 12_000


def _tool_message(tool_name: str, payload: dict[str, Any]) -> ChatMessage:
    content = json.dumps(payload)
    if len(content) > _TOOL_OUTPUT_CAP:
        content = content[:_TOOL_OUTPUT_CAP] + f"… [truncated; {len(content)} bytes total]"
    return ChatMessage(role="tool", name=tool_name, content=content)
