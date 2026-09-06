"""Agent runtime coordinator — the ONE cognitive authority.

Sali is one person doing one thing at a time. Messages arrive from the terminal, the iPhone, the
scheduler, perception and his own initiative; every one of them is arbitrated here, and exactly one
of them is thinking at any moment. That is not a throughput compromise — it is what makes him a
single mind rather than a committee sharing a database.

Two priorities, one slot:

* **Foreground** — Almir is waiting. Serialised by an async queue *and*, across processes, by the
  PostgreSQL-backed ``ExecutionLease``.
* **Background** — Sali's own life: scheduled work, investigation, learning, curiosity. It runs in
  the same slot, at lower priority, and *yields* the instant foreground work is submitted.

The single ``_cognition`` lock is what makes "one thing at a time" true rather than aspirational.
Background work used to run ``AgentLoop.astream`` directly, outside this file entirely, so a
scheduled turn and a user's turn could drive the same loop — the same task store, the same context
engine, the same publisher — concurrently. Now the only way into the loop is through here.

Yielding is a *cancel*, not a pause, and that is deliberate: background turns are journaled and
their tasks are durable, so an interrupted one resumes from persisted state, while a background
turn that merely "paused" would still be holding the model. "Sali, stop that" must actually stop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sali.core.ids import new_id
from sali.obs.log import get_logger
from sali.runtime.lease import HEARTBEAT_INTERVAL_SECONDS, ExecutionLease

log = get_logger("sali.runtime.coordinator")

# How often a running turn checks the lease for a cross-process preemption request (Prompt 2). Fast so
# an interrupt from another process (iPhone/API) feels responsive; the run stops at the next safe boundary.
_PREEMPT_POLL_SECONDS = 1.5


class ExecutionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"


class ExecutionOrigin(StrEnum):
    CLI = "cli"
    API = "api"
    IOS = "ios"
    SCHEDULER = "scheduler"
    RECOVERY = "recovery"


@dataclass(slots=True)
class ExecutionContext:
    """The current or most recent execution."""
    run_id: UUID
    session_id: UUID
    message: str
    origin: ExecutionOrigin
    started_at: float
    status: ExecutionStatus = ExecutionStatus.RUNNING
    cancelled: bool = False


class AgentRuntimeCoordinator:
    """The one cognitive authority: every turn Sali thinks passes through here.

    Foreground turns are serialised in this process by a queue and across processes by the
    PostgreSQL ``ExecutionLease``. Background turns — Sali's own life — share the same single
    cognition slot at lower priority and yield to the foreground on demand. Nothing reaches the
    ``AgentLoop`` by any other route.
    """

    def __init__(self, lease: ExecutionLease | None = None) -> None:
        self._current: ExecutionContext | None = None
        self._queue: asyncio.Queue[
            tuple[str, ExecutionOrigin, asyncio.Future[dict[str, Any]], Any]
        ] = asyncio.Queue()
        self._cancel_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._agent_loop: Any = None
        self._session_id: UUID | None = None
        self._ws_broadcaster: Any = None
        self._lease = lease
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._preempt_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[dict[str, Any]] | None = None  # the in-flight turn (cancellable)
        self._publisher: Any = None  # EventPublisher — set by runtime
        # ── The one cognition slot ───────────────────────────────────────────────────────────────
        # Held for the duration of any agent turn, foreground or background. One mind, one thought.
        self._cognition = asyncio.Lock()
        self._fg_waiting = 0                                   # foreground turns queued or running
        self._bg_task: asyncio.Task[dict[str, Any]] | None = None
        self._bg_current: ExecutionContext | None = None
        # Host stewardship: read the real machine before starting Sali's OWN work (never before
        # Almir's). Set by AgentRuntime, which also supplies the incident store.
        self._resources: Any = None
        self._incidents: Any = None
        self._last_incident_at: float = 0.0

    def set_agent_loop(self, loop: Any) -> None:
        self._agent_loop = loop

    def set_session_id(self, session_id: UUID) -> None:
        self._session_id = session_id

    def set_broadcaster(self, broadcaster: Any) -> None:
        self._ws_broadcaster = broadcaster

    def set_lease(self, lease: ExecutionLease) -> None:
        self._lease = lease

    @property
    def current_execution(self) -> ExecutionContext | None:
        return self._current

    @property
    def is_busy(self) -> bool:
        return self._current is not None and self._current.status == ExecutionStatus.RUNNING

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def foreground_demanded(self) -> bool:
        """Is anyone waiting on a foreground turn? Background cognition yields while this is true."""
        return self._fg_waiting > 0

    @property
    def background_execution(self) -> ExecutionContext | None:
        return self._bg_current

    @property
    def is_thinking(self) -> bool:
        """Is the one cognition slot occupied at all — by foreground OR background work?"""
        return self._cognition.locked()

    async def submit(
        self, message: str, origin: ExecutionOrigin, *,
        timeout: float = 300.0,
        event_callback: Any = None,
    ) -> dict[str, Any]:
        """Submit a message for foreground execution. Blocks until the turn completes.

        If another turn is running (in this or another process), this message is queued.
        Returns the execution result.
        """
        # Almir is waiting from this instant: claim priority BEFORE queueing, so a background turn
        # already thinking yields the model now rather than after the queue is drained.
        self._fg_waiting += 1
        await self._yield_background(f"foreground {origin.value} message")
        try:
            future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
            await self._queue.put((message, origin, future, event_callback))

            if self._loop_task is None or self._loop_task.done():
                self._loop_task = asyncio.create_task(self._process_loop())

            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                future.cancel()
                return {"error": "execution timed out", "status": "timeout"}
        finally:
            self._fg_waiting -= 1

    # ── Background cognition (same mind, lower priority) ──────────────────────────────────────────
    async def submit_background(
        self, message: str, *, session_id: UUID | None = None, timeout: float = 600.0,
        priority: str = "background",
    ) -> dict[str, Any]:
        """Run one of Sali's OWN turns — scheduled work, investigation, learning, curiosity.

        Same loop, same mind, same journal as a foreground turn; it simply never takes precedence
        over Almir. Returns ``deferred`` if foreground work is pending (it is not queued — the
        faculty that asked will come round again), and ``yielded`` if foreground work arrived while
        it was thinking.

        Background turns deliberately do NOT take the cross-process ``ExecutionLease``: that lease
        exists so two *foreground* turns can never both hold the conversation, and making Sali's own
        life compete for it would let a long background turn block Almir at the door. The single
        ``_cognition`` slot is what keeps them from overlapping.
        """
        if self._agent_loop is None or (self._session_id is None and session_id is None):
            return {"error": "agent not initialized", "status": "error"}
        if self.foreground_demanded:
            return {"status": "deferred", "reason": "foreground has priority"}

        # Sali does not endanger the machine he lives on. Every background turn — scheduled work,
        # investigation, learning, curiosity — passes this one check, because every background turn
        # now comes through here. Almir's foreground is deliberately NOT gated: under pressure Sali
        # sheds his own work and keeps answering.
        shed = await self._resource_verdict(priority, message)
        if shed is not None:
            return shed

        run_id = new_id()
        bg_session = session_id or self._session_id
        assert bg_session is not None  # noqa: S101 - guarded above; narrows for mypy
        async with self._cognition:
            if self.foreground_demanded:      # claimed while we waited for the slot
                return {"status": "deferred", "reason": "foreground has priority",
                        "run_id": str(run_id)}
            self._bg_current = ExecutionContext(
                run_id=run_id, session_id=bg_session, message=message,
                origin=ExecutionOrigin.SCHEDULER, started_at=time.time())
            await self._broadcast_lifecycle("cognition.background_started", {
                "run_id": str(run_id), "message_preview": message[:100]})
            task = asyncio.create_task(self._execute_background(message, bg_session, run_id))
            self._bg_task = task
            try:
                done, _pending = await asyncio.wait({task}, timeout=timeout)
                if not done:
                    task.cancel()
                    result: dict[str, Any] = {"status": "timeout", "run_id": str(run_id)}
                elif task.cancelled():
                    # Cancelled means it yielded to Almir. The task's own state is durable, so the
                    # faculty that owns it resumes from PostgreSQL, not from memory.
                    result = {"status": "yielded", "run_id": str(run_id)}
                else:
                    exc = task.exception()
                    result = ({"error": str(exc)[:200], "status": "error", "run_id": str(run_id)}
                              if exc is not None else task.result())
            finally:
                self._bg_task = None
                self._bg_current = None
            await self._broadcast_lifecycle("cognition.background_finished", {
                "run_id": str(run_id), "status": result.get("status")})
            return result

    async def _resource_verdict(self, priority: str, message: str) -> dict[str, Any] | None:
        """None to proceed, or the shed result. Deterministic: a measured reading → a state ladder →
        run/defer/reject by priority (:mod:`sali.runtime.resources`). Never the model's opinion.

        A dangerous host is answered by doing LESS, never by finding another way to run the work —
        which is why this lives above the cognition slot rather than beside it.
        """
        if self._resources is None:
            return None
        from sali.runtime.resources import ResourceAuthority
        try:
            state = await self._resources.state()
        except Exception:  # noqa: BLE001 - an unreadable machine must not stop Sali's life
            return None
        decision = ResourceAuthority().decide(priority=priority, state=state)
        if decision.verdict == "run":
            return None
        await self._record_incident(state, decision, message)
        log.info("background_shed_for_resources", verdict=decision.verdict, state=state.value,
                 priority=priority)
        await self._broadcast_lifecycle("cognition.background_shed", {
            "verdict": decision.verdict, "state": state.value, "priority": priority,
            "reasons": decision.reasons})
        return {"status": "deferred" if decision.verdict == "defer" else "rejected",
                "reason": f"host {state.value}: " + "; ".join(decision.reasons),
                "resource_state": state.value}

    async def _record_incident(self, state: Any, decision: Any, message: str) -> None:
        """Durably record host pressure that actually changed what Sali did — negative operational
        knowledge he can retrieve before planning the same workload again. Rate-limited, because an
        incident every poll is noise, not memory."""
        from sali.runtime.resources import ResourceAuthority
        if self._incidents is None or not ResourceAuthority().should_preserve(state):
            return
        now = time.monotonic()
        if now - self._last_incident_at < 600:      # at most one per 10 minutes
            return
        self._last_incident_at = now
        with contextlib.suppress(Exception):
            reading = await self._resources.observe()
            await self._incidents.record(
                kind="resource_pressure",
                severity="critical" if state.value == "emergency" else "high",
                workload=f"background cognition: {message[:100]}",
                observed={"state": state.value, "verdict": decision.verdict,
                          "reasons": decision.reasons, **reading.to_dict()})

    _YIELD_GRACE = 5.0   # seconds a well-behaved bg tool has to honor CancelledError

    async def _yield_background(self, reason: str) -> bool:
        """Hand the cognition slot back to the foreground by stopping the background turn.

        Cancels the bg task and awaits it under a grace window (`_YIELD_GRACE`, default 5s).
        If the task honors CancelledError promptly, returns True and the cognition slot is
        released cleanly. If the grace expires — a tool has swallowed the CancelledError or
        is doing sync work with no await point — the wedge is logged AT ERROR, a
        `runtime.background_wedged` event is published so operators/iOS see it, and False is
        returned. The caller then knows the slot may still be held and can decide policy
        (submit() still awaits `_cognition` in `_process_loop`, so the wedge blocks chat —
        but at least it is now VISIBLE instead of an invisible hang).
        """
        task = self._bg_task
        if task is None or task.done():
            return True
        log.info("background_yielding", reason=reason)
        task.cancel()
        # Use asyncio.wait (not wait_for + shield) so we neither swallow the task's own
        # CancelledError nor propagate it up as our own exit reason. wait tells us purely:
        # did the task finish within grace? (from any cause - cancel or natural).
        done, _pending = await asyncio.wait({task}, timeout=self._YIELD_GRACE)
        if task in done:
            return True
        try:
            raise TimeoutError()
        except (TimeoutError, asyncio.TimeoutError):
            log.error("background_yield_stuck",
                      reason=reason, grace=self._YIELD_GRACE)
            if self._publisher is not None:
                with contextlib.suppress(Exception):
                    from sali.events.publisher import SaliEvent
                    await self._publisher.publish(SaliEvent(
                        event_type="runtime.background_wedged",
                        origin="runtime",
                        data={"reason": reason, "grace_seconds": self._YIELD_GRACE,
                              "note": "a background tool did not honor CancelledError; the "
                                       "cognition slot may still be held. Chat may block until "
                                       "the wedged task times out or the daemon restarts."}))
            return False
        except asyncio.CancelledError:
            # We were cancelled while awaiting the yield - propagate.
            raise
        except Exception as exc:  # noqa: BLE001 - never let yield itself take the runtime down
            log.warning("background_yield_await_error", error=str(exc)[:200])
            return True   # the task exited (even if with an error), the slot will free

    async def _execute_background(
        self, message: str, session_id: UUID, run_id: UUID,
    ) -> dict[str, Any]:
        """One background turn through the SAME AgentLoop, checking for foreground demand between
        events so a yield is felt within a reasoning cycle rather than at the end of the turn."""
        from sali.events.publisher import SaliEvent

        final_text = ""
        tool_calls = 0
        try:
            async for event in self._agent_loop.astream(
                message, session_id=session_id, run_id=run_id, internal=True,
            ):
                if self.foreground_demanded:
                    return {"status": "yielded", "run_id": str(run_id), "text": final_text[:500]}
                if self._publisher is not None:
                    with contextlib.suppress(Exception):
                        await self._publisher.publish(SaliEvent(
                            event_type=f"agent.{event.kind}", run_id=run_id,
                            session_id=session_id, origin="agent",
                            data={"text": event.text, "background": True, **event.data}))
                if event.kind == "final":
                    final_text = event.text
                elif event.kind == "tool" and event.data.get("phase") == "start":
                    tool_calls += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a faculty's turn failing never kills the faculty
            return {"error": str(exc)[:200], "status": "error", "run_id": str(run_id)}
        return {"status": "completed", "run_id": str(run_id), "text": final_text[:500],
                "tool_calls": tool_calls}

    async def cancel_current(self) -> bool:
        """Cancel the currently running execution. Returns True if cancelled."""
        if self._current is None or self._current.status != ExecutionStatus.RUNNING:
            return False
        self._current.status = ExecutionStatus.CANCELLING
        self._current.cancelled = True
        self._cancel_event.set()          # cooperative signal (checked between astream events)
        # Hard-cancel the in-flight turn so a long-running tool is actually interrupted: the CancelledError
        # propagates into the tool's awaited subprocess, and exec.py kills the process group on cancel
        # (Final audit §6 — "stop" must stop already-running work, not just the next reasoning cycle).
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        log.info("execution_cancelling", run_id=str(self._current.run_id))
        return True

    async def _process_loop(self) -> None:
        """Process queued messages one at a time, with global lease coordination."""
        while True:
            try:
                message, origin, future, event_callback = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0)
            except TimeoutError:
                if self._queue.empty():
                    break
                continue
            except asyncio.CancelledError:
                break

            if self._agent_loop is None or self._session_id is None:
                future.set_result({"error": "agent not initialized", "status": "error"})
                continue

            run_id = new_id()

            # Acquire global foreground lease (cross-process coordination)
            if self._lease is not None:
                acquired = await self._lease.try_acquire(
                    run_id, self._session_id, origin.value)
                if not acquired:
                    # Another process owns foreground execution — wait and retry
                    log.info("lease_busy_waiting", run_id=str(run_id)[:8], origin=origin.value)
                    # Re-queue the message and wait
                    await self._queue.put((message, origin, future, event_callback))
                    await asyncio.sleep(1.0)
                    continue

            # THE cognition slot: one turn thinks at a time, foreground or background. A background
            # turn holding it was already asked to yield in submit(); we simply wait for it to let go.
            async with self._cognition:
                self._current = ExecutionContext(
                    run_id=run_id,
                    session_id=self._session_id,
                    message=message,
                    origin=origin,
                    started_at=time.time(),
                )
                self._cancel_event.clear()

                # Start heartbeat to keep lease alive during execution
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(run_id))
                # Fast cross-process preemption poll: an interrupt in another process yields us this run.
                self._preempt_task = asyncio.create_task(self._preempt_poll(run_id))

                await self._broadcast_lifecycle("execution.started", {
                    "run_id": str(run_id),
                    "origin": origin.value,
                    "message_preview": message[:100],
                })

                result: dict[str, Any] = {}
                try:
                    # Run the turn as a real task so cancel_current() can hard-cancel it (killing an in-flight
                    # tool subprocess), not merely set a cooperative flag checked between reasoning cycles.
                    self._turn_task = asyncio.create_task(
                        self._execute_turn(message, run_id, event_callback=event_callback))
                    result = await self._turn_task
                except asyncio.CancelledError:
                    result = {"status": "cancelled", "run_id": str(run_id)}
                except Exception as exc:
                    result = {"error": str(exc)[:200], "status": "error", "run_id": str(run_id)}
                    log.error("execution_failed", run_id=str(run_id), error=str(exc))
                finally:
                    # Stop heartbeat
                    if self._heartbeat_task is not None:
                        self._heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self._heartbeat_task
                        self._heartbeat_task = None
                    if self._preempt_task is not None:
                        self._preempt_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self._preempt_task
                        self._preempt_task = None
                    self._turn_task = None

                    # Release global lease
                    if self._lease is not None:
                        with contextlib.suppress(Exception):
                            await self._lease.release()

                    status = "cancelled" if self._current.cancelled else result.get("status", "completed")
                    self._current.status = ExecutionStatus.IDLE
                    await self._broadcast_lifecycle("execution.completed", {
                        "run_id": str(run_id),
                        "status": status,
                        "origin": origin.value,
                    })
                    self._current = None
            if not future.cancelled():
                future.set_result(result)

    async def _heartbeat_loop(self, run_id: UUID) -> None:
        """Periodically refresh the lease heartbeat during execution — and stamp agent_runs.updated_at so a
        genuinely-live run (e.g. one blocked in a long tool call that emits no journal events) is never seen
        as 'stale' and reclaimed+re-driven by another starting process's recovery (Final audit §34)."""
        if self._lease is None:
            return
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                still_owned = await self._lease.heartbeat()
                with contextlib.suppress(Exception):
                    if self._lease is not None and getattr(self._lease, "_pool", None) is not None:
                        async with self._lease._pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE agent_runs SET updated_at = now() "
                                "WHERE run_id = $1 AND status = 'running'", run_id)
                if not still_owned:
                    log.warning("lease_lost_during_execution", run_id=str(run_id)[:8])
                    # Another process stole the lease — cancel this execution
                    self._cancel_event.set()
                    if self._turn_task is not None and not self._turn_task.done():
                        self._turn_task.cancel()  # hard-stop the turn so it can't keep running lease-less
                    break
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass  # heartbeat failure is non-fatal

    async def _preempt_poll(self, run_id: UUID) -> None:
        """Poll the lease for a cross-process preemption request; if another process asked us to yield the
        foreground, cancel this run at the next safe boundary (§5). Fast so interruption feels responsive."""
        if self._lease is None:
            return
        while True:
            try:
                await asyncio.sleep(_PREEMPT_POLL_SECONDS)
                if await self._lease.preempt_requested():
                    log.info("execution_preempted", run_id=str(run_id)[:8])
                    self._cancel_event.set()
                    break
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 - preemption polling must never crash a run
                pass

    async def _execute_turn(
        self, message: str, run_id: UUID, *, event_callback: Any = None,
    ) -> dict[str, Any]:
        """Execute one agent turn, respecting cancellation.

        Events are delivered through the canonical publisher (durable + live).
        The event_callback is for terminal streaming — it receives the same events.
        """
        from sali.events.publisher import SaliEvent

        events: list[dict[str, Any]] = []
        final_text = ""
        tool_calls = 0

        try:
            async for event in self._agent_loop.astream(
                message, session_id=self._session_id, run_id=run_id,
            ):
                if self._cancel_event.is_set():
                    return {"status": "cancelled", "run_id": str(run_id), "events": events}

                # Publish through canonical publisher (durable + WebSocket + subscribers)
                if self._publisher is not None:
                    with contextlib.suppress(Exception):
                        await self._publisher.publish(SaliEvent(
                            event_type=f"agent.{event.kind}",
                            run_id=run_id,
                            session_id=self._session_id,
                            origin="agent",
                            data={"text": event.text, **event.data},
                        ))

                # Terminal callback (same events, different delivery)
                if event_callback is not None:
                    with contextlib.suppress(Exception):
                        event_callback(event)

                events.append({
                    "kind": event.kind,
                    "text": event.text[:200],
                    "data_keys": list(event.data.keys()),
                })

                if event.kind == "final":
                    final_text = event.text
                elif event.kind == "tool" and event.data.get("phase") == "start":
                    tool_calls += 1

        except asyncio.CancelledError:
            return {"status": "cancelled", "run_id": str(run_id), "events": events}
        except Exception as exc:
            return {"error": str(exc)[:200], "status": "error", "run_id": str(run_id), "events": events}

        return {
            "status": "completed",
            "run_id": str(run_id),
            "text": final_text[:500],
            "tool_calls": tool_calls,
            "events_count": len(events),
        }

    async def _broadcast_lifecycle(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish lifecycle event through canonical publisher."""
        if self._publisher is not None:
            from sali.events.publisher import SaliEvent
            with contextlib.suppress(Exception):
                await self._publisher.publish(SaliEvent(
                    event_type=event_type,
                    run_id=data.get("run_id"),
                    session_id=self._session_id,
                    origin="agent",
                    data=data,
                ))
        elif self._ws_broadcaster:
            # Fallback: direct broadcast (backward compatibility)
            with contextlib.suppress(Exception):
                await self._ws_broadcaster.broadcast_event(event_type, data)
