"""Agent runtime coordinator — single point of execution control.

Ensures only ONE agent turn executes at a time across ALL processes (CLI, API, iOS).
Uses PostgreSQL-backed ExecutionLease for cross-process coordination.

The coordinator:
- Acquires global foreground lease before execution
- Heartbeats during execution to keep lease alive
- Releases lease on completion/failure/cancellation
- Serializes incoming messages via an async queue
- Provides safe cancellation
- Publishes lifecycle events to WebSocket clients
- Tracks run_id, origin, and status
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
    """Serializes agent execution across all processes.

    Only ONE foreground turn runs at a time globally (enforced by PostgreSQL lease).
    New messages are queued and processed in order. The active turn can be safely cancelled.
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

    async def submit(
        self, message: str, origin: ExecutionOrigin, *,
        timeout: float = 300.0,
        event_callback: Any = None,
    ) -> dict[str, Any]:
        """Submit a message for foreground execution. Blocks until the turn completes.

        If another turn is running (in this or another process), this message is queued.
        Returns the execution result.
        """
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        await self._queue.put((message, origin, future, event_callback))

        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._process_loop())

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            future.cancel()
            return {"error": "execution timed out", "status": "timeout"}

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
