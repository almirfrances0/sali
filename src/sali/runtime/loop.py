"""The agent execution loop — the explicit FSM at Sali's core.

INPUT → RETRIEVE → BUILD_CONTEXT → REASON_PLAN → [AWAIT_CONFIRM → EXECUTE_TOOL → OBSERVE →
VERIFY → UPDATE_STATE]* → LEARN → RESPOND. The LLM is called at exactly one hot-loop site
(REASON_PLAN); retrieve, dispatch, verify, and journaling are deterministic. Every tool
effect is journaled ``executing`` *before* it runs (fix M15) and verified after — a tool is
never assumed to have succeeded (rule 13).
"""

from __future__ import annotations

import json
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
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

# Warmer sampling so Sali sounds like a person, not a deterministic tool. Tool-calling is
# rendered structurally by the model, so it still works reliably at this temperature.
_VOICE: dict[str, Any] = {
    "temperature": 0.7, "top_k": 40, "top_p": 0.95, "presence_penalty": 0.3, "repeat_penalty": 1.1,
}


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
                assembled = self.context.assemble(
                    user_input, bundle, specs,
                    live_note=LIVE_NOTE if plan.needs_live else None, history=history,
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

                while iteration < max_iter and tokens_used < budget:
                    await journal.set_state(RunState.REASON_PLAN, iteration=iteration)
                    yield LoopEvent("status", "thinking")
                    started = self.clock.now()
                    res: ChatResult | None = None
                    async for chunk in self.provider.chat_stream(
                        messages, tools=specs or None, options=_VOICE
                    ):
                        if chunk.thinking:
                            yield LoopEvent("thinking", chunk.thinking)
                        if chunk.content:
                            yield LoopEvent("token", chunk.content)
                        if chunk.done:
                            res = chunk.result
                    if res is None:
                        raise ProviderError("model stream ended without a result")
                    latency = int((self.clock.now() - started).total_seconds() * 1000)
                    tokens_used += res.tokens_in + res.tokens_out
                    await journal.event(
                        "llm_call", {"model": res.model, "tool_calls": len(res.tool_calls)},
                        latency_ms=latency, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                    )
                    if not res.tool_calls:
                        final_text = res.content
                        break
                    messages.append(
                        ChatMessage(role="assistant", content=res.content, tool_calls=res.tool_calls)
                    )
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
        verify = await tool.verify(call.arguments, result)
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
        rows = await conn.fetch(
            "SELECT role, content FROM message WHERE conversation_id=$1 ORDER BY seq DESC LIMIT 6",
            session_id,
        )
        return [(r["role"], r["content"]) for r in reversed(rows)]

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


def _tool_message(tool_name: str, payload: dict[str, Any]) -> ChatMessage:
    return ChatMessage(role="tool", name=tool_name, content=json.dumps(payload))
