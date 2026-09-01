"""AgentRuntime — the single execution authority for Sali.

Owns:
- ONE AgentLoop instance
- ONE execution coordinator (foreground serialization)
- ONE ExecutionLease (cross-process coordination)
- TaskAuthority (deterministic task management)
- Session identity

All foreground execution (terminal, API, iOS) goes through submit_foreground().
Background execution (scheduler, daemon) goes through submit_background().
There is never more than one foreground turn executing at a time — globally across processes.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sali.runtime.cognitive import CognitiveState

from sali.core.ids import new_id
from sali.events.publisher import EventPublisher
from sali.obs.log import get_logger
from sali.runtime import attention
from sali.runtime.coordinator import AgentRuntimeCoordinator, ExecutionOrigin
from sali.runtime.lease import ExecutionLease
from sali.runtime.loop import AgentLoop
from sali.tasks.inbox import MessageInbox
from sali.tasks.watchdog import TaskWatchdog, WatchdogConfig

log = get_logger("sali.runtime")


class AgentRuntime:
    """The single execution authority for a Sali process.

    Terminal, API, and iOS all submit through this runtime.
    The runtime owns one AgentLoop, one Coordinator, and one ExecutionLease.
    The lease ensures cross-process serialization via PostgreSQL.
    """

    def __init__(
        self,
        loop: AgentLoop,
        session_id: UUID,
        pool: Any,
        *,
        ws_broadcaster: Any = None,
        watchdog_config: WatchdogConfig | None = None,
    ) -> None:
        self._loop = loop
        self._session_id = session_id
        self._pool = pool

        # Canonical event publisher — single path for all events
        self._publisher = EventPublisher(pool)
        if ws_broadcaster:
            self._publisher.set_ws_manager(ws_broadcaster)

        # Create the global execution lease (PostgreSQL-backed)
        self._lease = ExecutionLease(pool)

        # Create the coordinator with the lease
        self._coordinator = AgentRuntimeCoordinator(lease=self._lease)
        self._coordinator.set_agent_loop(loop)
        self._coordinator.set_session_id(session_id)
        self._coordinator._publisher = self._publisher
        if ws_broadcaster:
            self._coordinator.set_broadcaster(ws_broadcaster)

        # Task progress watchdog — detects potentially stuck tasks
        self._watchdog = TaskWatchdog(pool, watchdog_config, publisher=self._publisher)

        # Durable attention inbox (Prompt 1) — incoming messages survive restart, are classified +
        # prioritised, and drive suspend/resume of the primary task via the same event pipeline.
        self._inbox = MessageInbox(pool, publisher=self._publisher)

        # Wire publisher into the loop and its task store
        loop._publisher = self._publisher
        loop._tasks._publisher = self._publisher
        loop._skills._publisher = self._publisher  # skill.discovered/selected/changed events (Prompt 5)
        loop._research_store._publisher = self._publisher  # research.* / learning.candidate_* events
        loop._decisions._publisher = self._publisher  # task.decision_* events (Prompt 6)
        loop._phases._publisher = self._publisher      # task.phase_* events (Prompt 6)
        loop._delegations._publisher = self._publisher  # delegation.* events (Cognitive OS §16)
        loop._questions._publisher = self._publisher    # task.waiting_for_user / user_answered (§44)

        # Reviewer gate (Prompt 4): the deterministic completion authority. Wired onto the loop (so the
        # finish_task/review_task tools reach it via ToolContext, and _open_tasks_note injects rework
        # findings) AND onto the task store (so BOTH completion choke points — finish('done') and the
        # advance auto-complete — require a passing review before a task can become 'done'). One
        # reviewer instance; the model can request a review but can never BE the reviewer.
        from sali.tasks.reviewer import TaskReviewer
        self._reviewer = TaskReviewer(pool, self._publisher)
        loop._reviewer = self._reviewer
        loop._tasks._reviewer = self._reviewer

        # Lifetime memory (Prompt: lifetime memory §18/§19): on a reviewer PASS, distil the task into
        # durable EXPERIENCE before the workspace/task graph is cleaned. Wired as the store's injected
        # hook so the tasks layer never imports the learning layer. The experience outlives the workspace.
        from sali.learning.experience import ExperienceStore
        self._experience = ExperienceStore(pool, self._publisher)
        loop._tasks._experience_hook = self._experience.extract_and_persist

    @property
    def loop(self) -> AgentLoop:
        return self._loop

    @property
    def coordinator(self) -> AgentRuntimeCoordinator:
        return self._coordinator

    @property
    def lease(self) -> ExecutionLease:
        return self._lease

    @property
    def session_id(self) -> UUID:
        return self._session_id

    @property
    def is_busy(self) -> bool:
        return self._coordinator.is_busy

    @property
    def current_execution(self) -> Any:
        return self._coordinator.current_execution

    async def cognitive_state(self) -> CognitiveState:
        """The unified, DERIVED CognitiveState (Cognitive OS §3/§4) — assembled fresh from durable
        sources, fully reconstructable from PostgreSQL. Owns nothing; reads everything."""
        from sali.runtime.cognitive import assemble
        return await assemble(
            self._pool, loop=self._loop, coordinator=self._coordinator, lease=self._lease,
            inbox=self._inbox, session_id=self._session_id)

    async def snapshot(self) -> dict[str, Any]:
        """A compact operational snapshot for observability / the iPhone controller (§46)."""
        return (await self.cognitive_state()).snapshot()

    async def send_agent_message(
        self, text: str, *, importance: str = "update", task_id: UUID | None = None,
    ) -> None:
        """Send an AGENT-originated message to the human via the event bus (§34/§35) — distinct from a
        reply to a user message and from a low-level system event. `importance` lets clients filter
        (progress / update / milestone / question / warning / completion / failure) so Sali informs
        without spamming. Uses the existing EventPublisher — no second transport."""
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type="agent.message", session_id=self._session_id, task_id=task_id,
                origin="agent", data={"text": text[:2000], "importance": importance,
                                      "channel": "agent_message"})

    def set_broadcaster(self, broadcaster: Any) -> None:
        self._coordinator.set_broadcaster(broadcaster)

    async def submit_foreground(
        self, message: str, origin: ExecutionOrigin, *, timeout: float = 600.0,
        event_callback: Any = None,
    ) -> dict[str, Any]:
        """Submit a foreground message. Serialized globally via PostgreSQL lease.

        If another process owns foreground execution, this message waits.
        Returns the execution result after the turn completes.
        """
        return await self._coordinator.submit(
            message, origin, timeout=timeout, event_callback=event_callback)

    async def submit_background(
        self, message: str, *, session_id: UUID | None = None, timeout: float = 600.0,
    ) -> dict[str, Any]:
        """Submit a background message. Uses a separate session if provided.

        Does NOT acquire the foreground lease — background work is independent.
        """
        bg_session = session_id or self._session_id
        run_id = new_id()
        result_text = ""
        tool_calls = 0

        try:
            async for event in self._loop.astream(message, session_id=bg_session, run_id=run_id):
                if event.kind == "final":
                    result_text = event.text
                elif event.kind == "tool" and event.data.get("phase") == "start":
                    tool_calls += 1
        except Exception as exc:
            return {"error": str(exc)[:200], "status": "error", "run_id": str(run_id)}

        return {
            "status": "completed",
            "run_id": str(run_id),
            "text": result_text[:500],
            "tool_calls": tool_calls,
        }

    async def cancel_foreground(self) -> bool:
        """Cancel the currently running foreground execution."""
        return await self._coordinator.cancel_current()

    async def recover(self) -> list[dict[str, str]]:
        """Recover orphaned runs from a prior crash."""
        # Also recover stale execution lease
        with contextlib.suppress(Exception):
            await self._lease.recover_stale()
        return await self._loop.recover()

    async def recover_tasks(self) -> list[dict[str, Any]]:
        """Recover orphaned tasks from a prior crash."""
        return await self._loop.recover_tasks()

    # ── Attention (Prompt 1) ──────────────────────────────────────────────────────────────────────
    @property
    def inbox(self) -> MessageInbox:
        return self._inbox

    async def route_incoming(
        self, message: str, *, origin: str = "cli", session_id: UUID | None = None,
        priority: str | None = None, dedup_key: str | None = None,
    ) -> dict[str, Any]:
        """Durably record + deterministically classify an incoming message (no LLM). Persists it to the
        inbox with its attention category + priority and emits attention.* events. The caller decides how
        to act on the returned category (answer / quick action / interrupt+suspend / replace / queue)."""
        sid = session_id or self._session_id
        primary = await self._loop._tasks.active_task()
        decision = attention.classify(
            message, has_primary=primary is not None,
            current_objective=(primary.objective if primary else ""))
        prio = priority or decision.priority.value
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type="attention.message_received", session_id=sid, origin=origin,
                subject_type="message", data={"content_preview": message[:100], "origin": origin})
        msg = await self._inbox.enqueue(
            message, session_id=sid, origin=origin, priority=prio,
            classification=decision.category.value,
            related_task_id=(primary.id if primary else None), dedup_key=dedup_key)
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type="attention.message_classified", session_id=sid, origin="attention",
                task_id=(primary.id if primary else None), subject_type="message",
                subject_id=(msg.id if msg else None),
                data={"category": decision.category.value, "priority": prio, "reason": decision.reason})
        return {
            "message_id": str(msg.id) if msg else None,
            "category": decision.category.value, "priority": prio,
            "has_primary": primary is not None,
            "primary_task_id": str(primary.id) if primary else None,
            "touches_primary": decision.touches_primary, "reason": decision.reason,
        }

    async def recover_attention(self) -> dict[str, Any]:
        """Deterministic attention recovery after a crash/restart (§14): re-queue any messages left
        mid-processing, then report the durable attention state (primary task, suspended?, queued)."""
        reclaimed = await self._inbox.reclaim_stale()
        snap = await attention.attention_snapshot(self._pool)
        snap["reclaimed_messages"] = reclaimed
        return snap

    async def suspend_primary(self, *, reason: str = "") -> Any:
        """Suspend the current primary task for an interruption (checkpoints are already durable)."""
        primary = await self._loop._tasks.active_task()
        if primary is None:
            return None
        return await self._loop._tasks.suspend(primary.id, reason=reason)

    async def resume_primary(self) -> Any:
        """Resume the suspended primary task from durable state — exactly where it stopped."""
        primary = await self._loop._tasks.active_task()
        if primary is None or primary.status != "paused":
            return None
        return await self._loop._tasks.resume(primary.id)

    async def handle_message(
        self, message: str, *, origin: str = "cli", session_id: UUID | None = None,
        event_callback: Any = None, priority: str | None = None, dedup_key: str | None = None,
    ) -> dict[str, Any]:
        """THE live routing authority (Prompt 2). Every foreground message (terminal/API/iOS) enters here —
        never coordinator.submit directly. Classifies deterministically, persists durably (idempotent), and
        runs the right lifecycle: answer without disturbing the task, or interrupt (yield the RUN + suspend
        the TASK) → do the work → automatically resume the primary from durable state. One foreground run at
        a time is preserved throughout (the lease stays authoritative)."""
        C = attention.AttentionCategory
        # Cognitive OS §44: if the primary task paused to ask the user a question, THIS message is the
        # answer — record it and put the task back to 'running' BEFORE classification, so the same task
        # resumes (never a new task, never a lost question).
        with contextlib.suppress(Exception):
            waiting = await self._loop._tasks.active_task()
            if waiting is not None and waiting.status == "waiting_for_user":
                answered = await self._loop._questions.answer(waiting.id, message)
                if answered:
                    await self._publisher.emit(
                        event_type="task.user_answered", task_id=waiting.id, session_id=session_id,
                        subject_type="task", subject_id=waiting.id, origin="runtime",
                        data={"resumed": True})
        # Prompt 11 §4/§57: if a consequential action is awaiting natural consent, THIS message may be the
        # consent signal ("yes, do it" / "only the first two" / "not now" / "leave it"). Interpret it and
        # resolve the pending consent — no y/n gate. Best-effort; an 'unclear' reply leaves it pending.
        with contextlib.suppress(Exception):
            primary_for_consent = await self._loop._tasks.active_task()
            if primary_for_consent is not None:
                from sali.tasks.consent import ConsentStore
                await ConsentStore(self._pool, self._publisher).resolve_pending_for_task(
                    primary_for_consent.id, message)
        # Prompt 12 §1/§2: if the user clearly ABANDONS the current work ("forget that project", "cancel
        # it", "I don't want that anymore"), that is a durable intent change — revoke the active task so no
        # background/recovery/continuation path can resurrect it. Conservative (a bare "stop" is an
        # interrupt, handled by attention, not a revocation). Best-effort, before routing.
        with contextlib.suppress(Exception):
            from sali.tasks.revocation import classify_revocation
            if classify_revocation(message):
                primary_to_revoke = await self._loop._tasks.active_task()
                if primary_to_revoke is not None:
                    from sali.runtime.revocation import revoke_intent
                    await revoke_intent(self._pool, primary_to_revoke.id, publisher=self._publisher)
        # Prompt 7 §6: explicit user feedback ("always use X", "don't do that again", "I prefer …") is
        # evidence — record a behavior CANDIDATE (never an automatic mutation; acceptance is separate and
        # authority-gated, §42). Deterministic, best-effort, and non-blocking to routing.
        with contextlib.suppress(Exception):
            from sali.learning.behavior import BehaviorStore
            await BehaviorStore(self._pool, self._publisher).observe_feedback(message)
        routing = await self.route_incoming(
            message, origin=origin, session_id=session_id, priority=priority, dedup_key=dedup_key)
        category = routing["category"]
        if routing["message_id"] is None:  # duplicate (dedup_key) — never execute twice (§15)
            return {"status": "duplicate", **routing}
        mid = UUID(routing["message_id"])

        if category == C.QUEUE_FOR_LATER.value:  # persist only; act after the primary finishes (§10)
            return {"status": "queued", **routing}

        interrupts = category in (C.INTERRUPT_TASK.value, C.QUICK_ACTION.value)
        is_cancel = category == C.CANCEL_PRIMARY_TASK.value      # "stop that" / "cancel that" / "abort that"
        is_replace = category == C.REPLACE_PRIMARY_TASK.value    # "actually, do X instead" — supersede
        # An explicit stop or replace MUST preempt the running turn NOW — never queue behind the very work it
        # is meant to stop (Final audit §5/§6). Cancel/replace are foreground-preempting, like interrupts.
        needs_foreground_now = (interrupts or is_cancel or is_replace
                                or category in (C.CONVERSATION.value, C.QUICK_ANSWER.value))
        primary = await self._loop._tasks.active_task()

        # Is the foreground busy — HERE or in another process (the durable lease is the authority, §16)?
        busy_here = self._coordinator.is_busy
        busy_global = not await self._lease.is_available()
        suspended = False
        if (busy_here or busy_global) and needs_foreground_now:
            await self._emit_attention("attention.primary_interrupted", session_id, mid,
                                       {"category": category, "reason": routing["reason"]}, primary)
            if busy_here:
                await self._coordinator.cancel_current()          # cancel the RUN (never the task here, §4)
            if busy_global:
                await self._lease.request_preemption(f"{category}: {message[:80]}")  # another process yields
            if interrupts and primary is not None:                 # suspend the TASK (durable) — not cancel
                await self._loop._tasks.suspend(primary.id, reason=f"{category}: {message[:80]}")
                suspended = True

        # An explicit CANCEL must DURABLY stop the task so no continuation/recovery/scheduler resumes it — the
        # run cancel above only stops the current turn (Final audit §6). We revoke the intent (tombstone +
        # cancel dependents + archive) so the abandoned task cannot come back. classify_revocation already
        # covers "forget that project"; this covers the attention-classified stop phrases it doesn't match.
        if is_cancel and primary is not None:
            with contextlib.suppress(Exception):
                from sali.runtime.revocation import is_resumable, revoke_intent
                if await is_resumable(self._pool, primary.id):  # not already revoked
                    await revoke_intent(self._pool, primary.id, reason="user_cancelled",
                                        publisher=self._publisher)
            await self._emit_attention("attention.primary_cancelled", session_id, mid,
                                       {"category": category}, primary)

        # Run this message as its OWN run through the coordinator (distinct run_id, lease-serialized).
        started = "attention.interrupt_run_started" if interrupts else "attention.turn_started"
        await self._emit_attention(started, session_id, mid, {"category": category}, primary)
        result = await self._coordinator.submit(
            message, _origin_enum(origin), timeout=600.0, event_callback=event_callback)
        await self._inbox.complete(mid, run_id=_run_id_of(result), classification=category)
        done = "attention.interrupt_run_completed" if interrupts else "attention.turn_completed"
        if interrupts and result.get("status") not in ("completed", "cancelled", None):
            done = "attention.interrupt_run_failed"
        await self._emit_attention(done, session_id, mid, {"status": result.get("status")}, primary)

        # Auto-resume the primary if (and only if) we suspended it and it's still resumable (§8/§9).
        resume = await self._maybe_resume_primary(primary, event_callback, session_id) if suspended else None
        return {"status": result.get("status", "completed"), "category": category,
                "result": result, "resume": resume, **routing}

    async def _maybe_resume_primary(
        self, previous: Any, event_callback: Any, session_id: UUID | None,
    ) -> dict[str, Any]:
        """Automatically resume the primary task after an interrupt — but ONLY from DURABLE state (§9):
        never if it was cancelled, superseded, completed, is no longer primary, or is not paused. Drives a
        NEW foreground resume run (its own run_id) with the durable resume context injected."""
        if previous is None:
            return {"resumed": False, "reason": "no primary task"}
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status, is_primary FROM task WHERE id = $1", previous.id)
        if row is None:
            return {"resumed": False, "reason": "task no longer exists"}
        if not row["is_primary"]:
            return {"resumed": False, "reason": "no longer primary (superseded/replaced)"}
        if row["status"] != "paused":
            return {"resumed": False, "reason": f"status '{row['status']}' — not resumable"}

        await self._loop._tasks.resume(previous.id)  # → running (emits task.resumed)
        ctx = await attention.resume_context(self._pool, previous.id)
        await self._emit_attention("attention.resume_run_started", session_id, None,
                                   {"task_id": str(previous.id)}, previous)
        resume_msg = ctx or (
            f"Continue the primary task: {previous.objective}. Work from the durable NEXT ACTION; "
            "do not restart completed work or repeat successful tool executions.")
        result = await self._coordinator.submit(
            resume_msg, ExecutionOrigin.RECOVERY, timeout=600.0, event_callback=event_callback)
        ev = "attention.resume_run_completed" if result.get("status") in ("completed", "cancelled", None) \
            else "attention.resume_run_failed"
        await self._emit_attention(ev, session_id, None, {"status": result.get("status")}, previous)
        return {"resumed": True, "run_status": result.get("status"), "run_id": result.get("run_id")}

    async def _emit_attention(
        self, event_type: str, session_id: UUID | None, message_id: UUID | None,
        data: dict[str, Any], task: Any,
    ) -> None:
        with contextlib.suppress(Exception):  # events are best-effort; never break the reflex
            await self._publisher.emit(
                event_type=event_type, session_id=session_id or self._session_id,
                task_id=(task.id if task is not None else None), subject_type="attention",
                subject_id=message_id, origin="attention", data=data)

    async def start(self) -> None:
        """Start runtime services (watchdog, etc.). Called once after construction."""
        await self._watchdog.start()
        log.info("watchdog_started")

    async def aclose(self) -> None:
        """Release runtime resources, watchdog, and lease."""
        await self._watchdog.stop()
        with contextlib.suppress(Exception):
            await self._lease.release()
        await self._loop.aclose()


def _origin_enum(origin: str) -> ExecutionOrigin:
    try:
        return ExecutionOrigin(origin)
    except ValueError:
        return ExecutionOrigin.CLI


def _run_id_of(result: dict[str, Any]) -> UUID | None:
    rid = result.get("run_id")
    if isinstance(rid, str):
        with contextlib.suppress(ValueError):
            return UUID(rid)
    return None
