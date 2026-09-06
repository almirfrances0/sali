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
import os
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

# "with X keep it brief" / "when talking to X, be direct" / "with X, don't be casual"
# Deliberately narrow: only preposition-anchored patterns where a Named Entity is the object,
# so a plain "I like Vim" doesn't get filed as a person preference.
_PERSON_PREF_RE = re.compile(
    r"\b(?:with|when (?:talking|writing|speaking) to|for)\s+"
    r"([A-Z][a-zA-Z][a-zA-Z\-']{1,30})\s*,?\s+"
    r"(?:keep it|be|don'?t be|please be|please)\s+"
    r"([a-zA-Z\- ]{3,30})",
    re.IGNORECASE,
)

if TYPE_CHECKING:
    from sali.runtime.cognitive import CognitiveState

from sali.events.publisher import EventPublisher
from sali.obs.log import get_logger
from sali.runtime import attention
from sali.runtime.coordinator import AgentRuntimeCoordinator, ExecutionOrigin
from sali.runtime.lease import ExecutionLease
from sali.runtime.loop import AgentLoop
from sali.tasks.inbox import MessageInbox
from sali.tasks.watchdog import TaskWatchdog, WatchdogConfig

log = get_logger("sali.runtime")


# Sali's OWN task-continuation turns run in this session, never Almir's conversation, so the chat is
# never polluted with internal prompts. Task progress reaches the app through task.* events.
# Tasks whose start has already been announced to Almir, so he is told once, not every turn.
_ANNOUNCED_TASKS: set[str] = set()

_TASK_WORK_SESSION = UUID("5a11c0de-0000-4000-8000-7461736b7773")


async def _capture_person_preference_impl(pool: Any, publisher: Any, message: str) -> None:
    """Detect per-person preference statements and file them via PersonStore.set_preference.

    Narrow trigger: "with Sasha keep it brief", "when talking to Marcus be direct". BehaviorStore
    catches the global variant ("keep it brief"); this catches the person-scoped one so
    behavioral_context.py can look up per-relationship register when composing the prompt.

    Idempotent — PersonStore.set_preference is jsonb-merge; a repeat statement just refreshes."""
    if not message or len(message) > 800:
        return
    match = _PERSON_PREF_RE.search(message)
    if match is None:
        return
    name, pref = match.group(1).strip(), " ".join(match.group(2).lower().split())
    if len(name) < 2 or len(pref) < 3:
        return
    from sali.tasks.people import PersonStore

    store = PersonStore(pool, publisher)
    with contextlib.suppress(Exception):
        # Upsert the person as 'explicit' (the user just named them with a stated preference).
        await store.upsert(name=name, provenance="explicit", confidence=0.8)
        await store.set_preference(name, key="register", value=pref, provenance="explicit")
        await store.record_interaction(name, note=f"preference: {pref}")


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
        # Step-progression backstop: how many continuation turns each (task, step) has been driven
        # without the model marking it. Lets the engine advance a step the weak model keeps working
        # but never calls advance_task on, so a task can't loop on one step forever. In-memory (a
        # restart resets it, which is fine — recovery re-drives from durable step state).
        self._step_drives: dict[tuple[str, int], int] = {}

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

        # Host stewardship (§8-§16/§20): the coordinator reads the real machine before starting any of
        # Sali's OWN work, and records pressure that actually changed what he did as durable negative
        # knowledge. Wired here because the coordinator is the single choke point every background
        # turn now passes through — one check covers scheduler, investigation, learning and curiosity.
        from sali.runtime.resources import ResourceMonitor
        from sali.tasks.incidents import ResourceIncidentStore
        self._resources = ResourceMonitor()
        self._coordinator._resources = self._resources
        self._coordinator._incidents = ResourceIncidentStore(pool, self._publisher)

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
        # `_delegations` removed (brain-audit turn 8): no subagent runtime.
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
        # The ONE provider, borrowed so the store can name the reusable skill a finished task
        # demonstrated. This is the store the task hook actually calls — patching the copy in
        # loop.py did nothing, and the capability came out named after its objective again.
        self._experience = ExperienceStore(pool, self._publisher, provider=loop.provider)
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
        # Ground a SELF-INITIATED message before it ships (§ proactive grounding): this path runs outside
        # a turn with no receipts, so strike only the receipt-free families — a capability Sali lacks, a
        # machine state the machine disproves — never action_done/file_send (which would over-strike a
        # legitimate "I finished X"). Fail-open, and struck strikes feed the same /grounding ledger.
        with contextlib.suppress(Exception):
            from sali.verify.response_claims import validate_proactive
            _rv = await validate_proactive(text, cap_of={})
            if _rv.changed:
                text = _rv.rewritten
                from sali.runtime.grounding_log import GroundingLog
                await GroundingLog(self._pool).record(
                    session_id=self._session_id, run_id=None, claims=_rv.struck)
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
        priority: str = "background",
    ) -> dict[str, Any]:
        """Submit one of Sali's OWN turns — scheduled work, investigation, learning, curiosity.

        Routed through the SAME coordinator as every foreground message, so background life shares
        the one cognition slot instead of driving the AgentLoop concurrently beside it. It never
        takes the foreground lease, and it yields to Almir the moment he speaks: the result may come
        back ``deferred`` (foreground pending, never started) or ``yielded`` (foreground arrived
        mid-turn). Both are normal — the task state is durable, so the faculty resumes from
        PostgreSQL rather than from memory. It can also come back ``rejected`` when the host is under
        dangerous pressure: Sali protects the machine he lives on by doing less, never by finding
        another way to run the work (`priority` picks where in that ladder this turn sits —
        'curiosity' yields first, 'background' next; Almir's foreground is never gated).
        """
        return await self._coordinator.submit_background(
            message, session_id=session_id or self._session_id, timeout=timeout, priority=priority)

    async def cancel_foreground(self) -> bool:
        """Cancel the currently running foreground execution — here or in ANY process (the durable
        lease is the authority; audit: a run held by the terminal was uncancellable from the phone
        because this only reached the in-process coordinator)."""
        cancelled = await self._coordinator.cancel_current()
        with contextlib.suppress(Exception):
            if not await self._lease.is_available():
                await self._lease.request_preemption("user cancel (api)")
                cancelled = True
        return cancelled

    async def recover(self) -> list[dict[str, str]]:
        """Recover orphaned runs from a prior crash."""
        # Also recover stale execution lease
        with contextlib.suppress(Exception):
            await self._lease.recover_stale()
        return await self._loop.recover()

    async def note_downtime(self) -> dict[str, Any]:
        """How long Sali was gone, and what came due while he was away. Startup-only — the evidence
        it reads is overwritten by the first turn."""
        return await self._loop.note_downtime()

    async def recover_tasks(self) -> list[dict[str, Any]]:
        """Recover orphaned tasks from a prior crash."""
        return await self._loop.recover_tasks()

    def note_connection(self, ctx: Any) -> None:
        """Record who Sali is talking to right now (channel + local/remote) so the mind's self-state can
        say where Almir is and how to reach him — in particular that a file must be SENT to a phone, not
        left at a local path. Best-effort, in-memory, per-turn (§17-28)."""
        self._current_connection = ctx
        with contextlib.suppress(Exception):
            self._loop._connection = ctx

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

    async def release_idle_model(self, *, vram_floor: float = 0.93) -> bool:
        """Drop the resident model when Sali is genuinely idle and the card is filling up.

        MEASURED: the KV cache grows across a session — 90.9% -> 93.6% -> 95.05% -> 96.7% of a 12 GB card
        in one afternoon. Nothing reclaimed it, so the card crept toward whatever ceiling was set and Sali
        began stalling against its own residency. Unloading resets the cache; the next turn reloads the
        model (~30s), which is why this only ever runs when NOTHING is happening: no foreground work, no
        active task, and the inference lease free. Almir never waits for this — a turn that arrives during
        the reload simply pays the load it would have paid after any idle eviction.
        """
        if self._coordinator.is_busy or not await self._lease.is_available():
            return False
        if await self._loop._tasks.active_task() is not None:
            return False
        from sali.provider.ollama import _gpu_snapshot

        snap = await _gpu_snapshot()
        if snap is None or snap[1] < vram_floor:
            return False
        import contextlib as _ctx

        import httpx
        with _ctx.suppress(Exception):
            async with httpx.AsyncClient(timeout=20.0) as c:
                await c.post(f"{self._loop.provider.s.host}/api/generate",
                             json={"model": self._loop.provider.s.chat_model, "keep_alive": 0})
            return True
        return False

    async def active_task_snapshot(self) -> tuple[str, int] | None:
        """(task_id, completed_step_count) for the active primary, or None. Lets a caller tell real
        progress from a treadmill without reaching into the task store itself."""
        primary = await self._loop._tasks.active_task()
        if primary is None:
            return None
        async with self._loop.pool.acquire() as conn:
            done = await conn.fetchval(
                "SELECT count(*) FROM task_step WHERE task_id=$1 AND status='done'", primary.id)
        return (str(primary.id), int(done or 0))

    async def continue_primary(self, *, timeout: float = 900.0) -> str | None:
        """Drive ONE continuation turn on the active primary task when nothing else needs the mind.

        Long work (multi-day coding tasks) previously advanced ONLY when Almir sent a message:
        ``_maybe_resume_primary`` is reachable from ``handle_message`` alone, so an unattended task
        simply STOPPED between turns, and a restart orphaned it outright. This is the autonomous
        driver for long-horizon work.

        It is deliberately conservative, and shares every guarantee ``drain_queued`` already relies on:
        the SAME coordinator and the SAME single cognition slot (never a second mind), never the
        foreground lease, and ``submit_background`` yields the instant Almir speaks. A task that is
        waiting on him, paused, blocked or finished is left alone — only 'running'/'open' is driven.

        Running a turn also refreshes the task heartbeat, which is what keeps a live task from ever
        LOOKING orphaned. That matters: recovery increments ``retry_count``, so orphan-recovery must
        stay a startup concern (see the daemon faculty) and must never be put on a timer.

        Progress surfaces through task.* events (the Tasks screen), not as fake chat: the turn runs in
        its own work session so Almir's conversation is never polluted with Sali's internal prompts.
        """
        if self._coordinator.is_busy or not await self._lease.is_available():
            return None
        primary = await self._loop._tasks.active_task()
        if primary is None or primary.status not in ("running", "open"):
            return None
        # The prompt MUST demand the step be marked. Measured: over 19 continuation turns of real work
        # (files written, tests run) `advance_task` was called only 8 times — and all 8 succeeded, so the
        # marking machinery is fine; it simply was not being invoked. Un-marked steps mean the Tasks
        # screen cannot show honest progress, the task can never auto-complete (completion is derived
        # from step state), and the stagnation guard sees a treadmill. Naming the tool and making the
        # mark the DEFINITION of finishing a step is what closes that gap.
        # TELL ALMIR. `send_agent_message` was wired end-to-end (publisher -> WS -> iOS chat + Inbox) and
        # had NEVER been called — 0 agent.message rows in 5,379 events. So work happened in total silence:
        # he had no way to know Sali had picked something up. Announced once per task, on the first
        # continuation turn, so he is told he is being worked for without being spammed every 45s.
        # Announce ONCE PER TASK, durably. An in-process set forgets across restarts, and every deploy
        # then re-announced the same task — Almir got "I'm working on this in the background: …" twice
        # for one job. The event log already knows whether he has been told, so ask it.
        # Announce ONCE PER TASK, durably. An in-process set forgets across restarts, and every deploy
        # then re-announced the same task — Almir got "I'm working on this in the background: …" twice
        # for one job. The event log already knows whether he has been told, so ask it. (send_agent_message
        # carries the task id in the PAYLOAD, not subject_id.)
        tid = str(primary.id)
        if tid not in _ANNOUNCED_TASKS:
            _ANNOUNCED_TASKS.add(tid)
            told = False
            with contextlib.suppress(Exception):
                async with self._loop.pool.acquire() as conn:
                    told = bool(await conn.fetchval(
                        "SELECT 1 FROM event WHERE event_type='agent.message' "
                        "AND payload->>'task_id' = $1 LIMIT 1", tid))
            if not told:
                await self.send_agent_message(
                    f"I'm working on this in the background: {primary.objective}. "
                    f"You can keep talking to me while I do — I'll tell you when it's done.",
                    importance="progress", task_id=primary.id)
        # NAME THE STEP. A generic four-point instruction was skimmed: successive turns each announced
        # "Step 1: create requirements.txt… it already exists, so I'll overwrite it", did the work, and
        # never marked it — so the next turn started step 1 again, forever. Telling Sali the exact step
        # number and description, and the exact call to close it, removes the ambiguity that let him
        # loop. The step is read fresh each turn, so this always points at real durable state.
        # STEP-DISCIPLINE: drive the first READY LEAF unit — a step/sub-step that is (a) pending/running,
        # (b) has every dependency done/skipped (respects the depends_on DAG, so a prerequisite is never
        # jumped), and (c) has NO unfinished children (a parent with sub-steps is never worked directly;
        # its sub-steps are driven one at a time and the parent auto-completes via the cascade when they
        # all finish). This replaces the naive `ORDER BY seq LIMIT 1`, which ignored both the DAG and the
        # hierarchy and could hand back a parent step whose real work lives in its sub-steps.
        nxt = None
        async with self._loop.pool.acquire() as conn:
            nxt = await conn.fetchrow(
                "SELECT s.seq, s.description, s.attempts, s.last_error, "
                "       s.definition_of_done, s.scope_excludes, s.parent_seq "
                "FROM task_step s WHERE s.task_id=$1 AND s.status IN ('pending','running') "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM unnest(s.depends_on) AS dep "
                "    WHERE dep NOT IN (SELECT seq FROM task_step "
                "                      WHERE task_id=$1 AND status IN ('done','skipped'))) "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM task_step c WHERE c.task_id=$1 AND c.parent_seq=s.seq "
                "      AND c.status IN ('pending','running','waiting')) "
                "ORDER BY s.seq LIMIT 1", primary.id)
        if nxt is not None:
            # ONGOING STEP, IN REALTIME. Mark the step we're about to drive as 'running' and announce
            # task.step.started, so the app shows the ONE step Sali is on RIGHT NOW instead of inferring
            # it. This is also the only place a step becomes 'running' — the real lifecycle is
            # pending → running (driver picks it) → done (advance_task marks it). Emitting only on the
            # genuine pending→running transition keeps it from re-announcing when the same step is
            # handed back for another turn.
            with contextlib.suppress(Exception):
                async with self._loop.pool.acquire() as conn:
                    _started = await conn.fetchval(
                        "UPDATE task_step SET status='running', "
                        "  started_at=coalesce(started_at, now()) "
                        "WHERE task_id=$1 AND seq=$2 AND status='pending' RETURNING seq",
                        primary.id, nxt["seq"])
                if _started is not None:
                    await self._publisher.emit(
                        event_type="task.step.started", task_id=primary.id,
                        session_id=_TASK_WORK_SESSION,  # so the app attributes it to task-work, not chat
                        subject_type="task", subject_id=primary.id, origin="runtime",
                        data={"step": nxt["seq"],
                              "description": str(nxt["description"])[:200]})
            # SHORT AND IMPERATIVE. A longer, well-reasoned instruction was answered
            # CONVERSATIONALLY: each turn produced one sentence ("Let me check if app.py already
            # exists.") and no tool call, so the loop saw an assistant message with no calls and ended
            # the turn — a whole continuation cycle burned on narration. Lead with the action, forbid
            # narration explicitly, and state the closing call twice; the local model follows a short
            # imperative far more reliably than a paragraph of policy.
            # SHOW HIM THE WORKSPACE. Every turn was being spent announcing "Let me check if app.py
            # already exists in the workspace:" and then ending — the model asked a question, the loop
            # saw a text-only response and finalised the turn, and the next turn asked the same
            # question. It is a cheap listing; answering it up front removes the exploratory round trip
            # entirely and lets the turn go straight to the work.
            listing = ""
            root = getattr(primary, "workspace_root", None)
            if root:
                with contextlib.suppress(Exception):
                    import os as _os

                    names: list[str] = []
                    for dirpath, dirnames, filenames in _os.walk(root):
                        dirnames[:] = [d for d in dirnames
                                       if d not in {".venv", ".git", "__pycache__", "node_modules"}]
                        for fn in filenames:
                            rel = _os.path.relpath(_os.path.join(dirpath, fn), root)
                            names.append(rel)
                            if len(names) >= 40:
                                break
                        if len(names) >= 40:
                            break
                    listing = ("\nFiles already in the workspace: "
                               + (", ".join(sorted(names)) if names else "(empty)") + "\n")
            # DON'T REPEAT A FAILED APPROACH. A step that has already failed carries its error; handing
            # the same instruction back unchanged just invites the same attempt. Show Sali what he tried
            # and require a DIFFERENT approach — and, once it has failed repeatedly, to stop and say what
            # he needs rather than grinding. (Almir: "if stuck he must think and find another way".)
            prior_failure = ""
            _att = int(nxt["attempts"] or 0)
            if _att > 0:
                _err = (nxt["last_error"] or "").strip().replace("\n", " ")[:240]
                prior_failure = (
                    f"\n\nYou have already attempted this step {_att} time(s)"
                    + (f'; the last failure was: "{_err}"' if _err else "")
                    + ". Do NOT repeat that approach. Try a genuinely different one — a different tool, "
                      "command, or order of work. Search the web if you lack the knowledge."
                )
                if _att >= 3:
                    prior_failure += (
                        " If a different approach is not obvious, stop now: call advance_task with "
                        "status='blocked' and say plainly what you need from Almir."
                    )
            # STEP IS THE JOB, OBJECTIVE IS CONTEXT. The old prompt led with the whole objective
            # ("Build production-grade SaaS ForgeDesk…") every turn, which tempted the model to build
            # toward ALL of it and run ahead of the step. Now THIS step is the entire job; the objective
            # is one line of context. definition_of_done is the completion bar (the engine also verifies
            # it before accepting 'done'); scope_excludes names what belongs to LATER steps so the model
            # is told, structurally, not to touch it.
            _dod = (nxt["definition_of_done"] or "").strip()
            _excl = (nxt["scope_excludes"] or "").strip()
            _dod_line = f"DONE WHEN: {_dod}\n" if _dod else ""
            _excl_line = (
                f"DO NOT touch this now (it belongs to a LATER step): {_excl}\n" if _excl else "")
            prompt = (
                f"You are working ONE step of a task. Do THIS STEP ONLY — nothing that belongs to a "
                f"later step.\n\n"
                f"STEP {nxt['seq']}: {nxt['description']}\n"
                f"{_dod_line}{_excl_line}"
                f"\nThis step is part of (context only, do NOT build the rest now): {primary.objective}\n"
                f"WORKSPACE: {root or '(none)'}{listing}"
                f"\nAct now, with tools. Do NOT describe what you are about to do — do it.\n"
                f"The listing above is authoritative — do NOT spend this turn checking what exists. "
                f"If this step's file is already listed, read it to confirm it is correct, then mark "
                f"the step done; otherwise create it now.\n"
                f"When this step is genuinely done"
                f"{f' (its DONE WHEN is satisfied)' if _dod else ''}, call "
                f"advance_task(step={nxt['seq']}, status='done'). The step stays open until you make "
                f"that call, and you will be handed the NEXT step automatically — do not start it here.\n"
                f"If it cannot be done: advance_task(step={nxt['seq']}, status='failed') with a "
                f"one-line reason, or status='blocked' if you need Almir."
                + prior_failure
            )
        else:
            prompt = ("(Continue your current task from its durable state. Every step is marked — if the "
                      "work is genuinely complete, call finish_task; otherwise report what remains.)")
        result = await self.submit_background(
            prompt, session_id=_TASK_WORK_SESSION, timeout=timeout, priority="background")
        # STEP-PROGRESSION BACKSTOP: after the turn, if the step Sali was driving is still open, the
        # ENGINE advances it — so a task moves one step at a time even when the weak model forgets to
        # call advance_task (observed live: it built the whole app under 'step 1', never marking it,
        # looping forever). Two triggers: (a) the step's definition_of_done is met (files present) —
        # safe, verified; (b) after several work-turns with no mark — a bounded force so it can't loop.
        if nxt is not None:
            with contextlib.suppress(Exception):
                await self._maybe_autoadvance_step(
                    primary.id, nxt, getattr(primary, "workspace_root", None))
        return str(result.get("status", "")) if isinstance(result, dict) else None

    async def _maybe_autoadvance_step(
        self, task_id: UUID, step_row: Any, workspace_root: str | None,
    ) -> None:
        """Engine-side step progression when the model won't mark a step itself. Advances the step
        only when it is safe (definition_of_done met) or, as a bounded backstop, after several
        work-turns without a mark — so a weak model can't wedge a task on one over-built step."""
        seq = int(step_row["seq"])
        async with self._loop.pool.acquire() as conn:
            cur = await conn.fetchrow(
                "SELECT status FROM task_step WHERE task_id=$1 AND seq=$2", task_id, seq)
        if cur is None or cur["status"] not in ("pending", "running"):
            self._step_drives.pop((str(task_id), seq), None)   # model marked it — done here
            return

        reason: str | None = None
        roots = [r for r in [workspace_root] if r]
        dod = ""
        with contextlib.suppress(Exception):
            dod = (step_row["definition_of_done"] or "").strip()
        # (a) SAFE: the step's definition_of_done names files that now all exist → advance.
        if dod:
            from sali.tools.builtins.task_tool import _extract_paths

            paths = _extract_paths(dod)
            if paths and roots and all(
                any(os.path.exists(p if os.path.isabs(p) else os.path.join(r, p)) for r in roots)
                for p in paths
            ):
                reason = "definition of done met (files present); advanced by the engine"

        # (b) BOUNDED FORCE: driven several times with real work in the workspace but never marked.
        if reason is None:
            key = (str(task_id), seq)
            self._step_drives[key] = self._step_drives.get(key, 0) + 1
            worked = bool(roots and os.path.isdir(roots[0]) and any(os.scandir(roots[0])))
            if self._step_drives[key] >= 3 and worked:
                reason = (f"advanced by the engine after {self._step_drives[key]} work-turns with no "
                          "explicit mark (backstop against looping on one step)")

        if reason is not None:
            with contextlib.suppress(Exception):
                await self._loop._tasks.advance(task_id, seq, "done", note=reason,
                                                run_id=None)
            with contextlib.suppress(Exception):
                await self._publisher.emit(
                    event_type="task.step.completed", task_id=task_id,
                    session_id=_TASK_WORK_SESSION, subject_type="task", subject_id=task_id,
                    origin="runtime", data={"seq": seq, "auto_advanced": True, "reason": reason})
            self._step_drives.pop((str(task_id), seq), None)

    async def drain_queued(self, *, event_callback: Any = None) -> int:
        """Act on QUEUE_FOR_LATER messages once Sali is idle (audit: "after you finish, do X" was
        persisted + classified and then never acted on by anything). Only when there is NO active
        primary and the foreground is free; each drained message runs as its own foreground run.
        Bounded per call — the caller loops on its own cadence."""
        drained = 0
        for _ in range(5):
            if self._coordinator.is_busy or not await self._lease.is_available():
                return drained
            if await self._loop._tasks.active_task() is not None:
                return drained
            msg = await self._inbox.claim_next_queued()
            if msg is None:
                return drained
            result = await self._coordinator.submit(
                f"(You set this aside until you finished your work; handle it now.) {msg.content}",
                _origin_enum(msg.origin), timeout=600.0, event_callback=event_callback)
            await self._inbox.complete(msg.id, run_id=_run_id_of(result))
            drained += 1
        return drained

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
        # DURABLE INTENT CHANGE. When Almir clearly abandons the current work ("forget that
        # project", "cancel it", "I don't want that anymore") the task is revoked so no
        # background/recovery/continuation path can resurrect it. Conservative on purpose: a bare
        # "stop" is an interrupt (handled by attention), not a durable revocation.
        #
        # Routed through IntentClassifier, the single seam that answers "does Almir want to cancel"
        # for the whole runtime. The fast path is the union of the existing regex modules (kept as
        # they are: today's fixes for "leave it", the "dont forget it" negation guard, the shared
        # cancellation detector all still flow through). The seam exists so a model-backed fallback
        # can be enabled later without touching this call site or the two others - understanding
        # here, enforcement below.
        with contextlib.suppress(Exception):
            from sali.core.intent import IntentClassifier, IntentKind
            if IntentClassifier().understands_cancellation(message).kind is IntentKind.CANCEL:
                primary_to_revoke = await self._loop._tasks.active_task()
                if primary_to_revoke is not None:
                    # Cancel the running turn FIRST so an in-flight tool actually stops before
                    # revoke_intent archives the task out from under it. Reversing this order
                    # (revoke → cancel) meant the tool kept running against a row already stamped
                    # archived_at, mutating state after the user said "leave it." Now: cut off
                    # the runaway work, then durably tombstone.
                    with contextlib.suppress(Exception):
                        await self._coordinator.cancel_current()
                    from sali.runtime.revocation import revoke_intent
                    await revoke_intent(self._pool, primary_to_revoke.id, publisher=self._publisher)
        # Prompt 7 §6: explicit user feedback ("always use X", "don't do that again", "I prefer …") is
        # evidence — record a behavior CANDIDATE (never an automatic mutation; acceptance is separate and
        # authority-gated, §42). Deterministic, best-effort, and non-blocking to routing.
        with contextlib.suppress(Exception):
            from sali.learning.behavior import BehaviorStore
            await BehaviorStore(self._pool, self._publisher).observe_feedback(message)
        # §23/§24: a preference about a specific PERSON ("with Sasha keep it brief", "when talking
        # to Marcus …") is per-person state, not global behavior — the BehaviorStore's global
        # candidate row would lose that scope. Route the person-scoped subset into PersonStore
        # additionally, so behavioral_context.py can look up per-relationship preferences.
        with contextlib.suppress(Exception):
            await _capture_person_preference_impl(self._pool, self._publisher, message)
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
        # A plain conversational message or quick answer sent while Sali is mid-work must be answered
        # promptly AND the interrupted task resumed afterward — otherwise cancelling the run below would
        # strand the task 'running' forever (interruption fix). So these join interrupts in the set that
        # PREEMPTS-AND-RESUMES. Cancel/replace also preempt, but deliberately END the old task, so they are
        # NOT in the resume set.
        preempts_and_resumes = (interrupts
                                or category in (C.CONVERSATION.value, C.QUICK_ANSWER.value,
                                                C.MODIFY_PRIMARY_TASK.value))  # Turn 6: modify preempts+resumes
        # An explicit stop or replace MUST preempt the running turn NOW — never queue behind the very work it
        # is meant to stop (Final audit §5/§6). Cancel/replace are foreground-preempting, like interrupts.
        needs_foreground_now = preempts_and_resumes or is_cancel or is_replace
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
            # Suspend the TASK (durable) whenever we preempt a run we must return to — not only for the two
            # "interrupt" categories. Guard on status=='running' so we never suspend work that isn't the live
            # turn (which would trigger a spurious resume).
            if preempts_and_resumes and primary is not None and primary.status == "running":
                await self._loop._tasks.suspend(primary.id, reason=f"{category}: {message[:80]}")
                suspended = True

        # An explicit CANCEL must DURABLY stop the task so no continuation/recovery/scheduler resumes it — the
        # run cancel above only stops the current turn (Final audit §6). We revoke the intent (tombstone +
        # cancel dependents + archive) so the abandoned task cannot come back. classify_revocation already
        # covers "forget that project"; this covers the attention-classified stop phrases it doesn't match.
        # REPLACE is included: "do X INSTEAD" abandons the old work as thoroughly as "cancel that" -
        # not just preempting the run, but propagating cancellation into every dependent commitment,
        # obligation, goal and initiative candidate that pointed at the old objective.
        if (is_cancel or is_replace) and primary is not None:
            with contextlib.suppress(Exception):
                from sali.runtime.revocation import is_resumable, revoke_intent
                if await is_resumable(self._pool, primary.id):  # not already revoked
                    _reason = "user_replaced" if is_replace else "user_cancelled"
                    await revoke_intent(self._pool, primary.id, reason=_reason,
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
