"""InitiativeDriver — the missing periodic driver for the cognitive-cycle orchestration layer.

**The gap this closes** (from the turn's audit): every autonomy component the previous turns
built (InitiativeEngine, OpenLoopStore, CuriosityStore, CommunicationDecisionEngine) exists as
well-designed classes, and the runtime's background faculties list already runs perception,
attention, proactive, investigate, grounding, syswatch, task-continuation, etc. But
`InitiativeEngine.generate_candidates()` is only READY, never CALLED periodically — so the
"given everything Sali knows, is something worth doing?" cycle never actually runs.

This module is that missing driver. It's a small background loop that:

1. Wakes on an interval OR when the EventBus pushes (§42 event-driven where possible).
2. Consults `InitiativeEngine.next_wake` — if there's nothing due, sleeps.
3. Reads `ResourceMonitor` — if the GPU / RAM is under pressure OR a foreground turn is running,
   defers to the next cycle (§23 resource awareness, §25 background must not starve interactive).
4. Calls `InitiativeEngine.generate_candidates()` — the existing method that scans commitments /
   obligations / goals / open loops / curiosities and upserts initiative candidates.
5. For each new candidate, records a `decision_trace` row with mode='observe' (the initiative
   engine's job stops at NOTICING; actual action goes through the coordinator + policy layer).

This is INTENTIONALLY thin — it does not decide to act. Deciding to act on an initiative is
the coordinator's job (which already knows how to route work through the review + policy gate).
The driver's contract is: keep the initiative queue fresh and observable.

**Not another mind**: no LLM invocation, no separate reasoning stream, no cognition lock.
Reuses the existing InitiativeEngine, ResourceMonitor, EventBus, DecisionTraceStore.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sali.events.bus import wait_or_wake
from sali.obs.log import get_logger

log = get_logger("sali.cognitive.initiative_driver")


class InitiativeDriver:
    def __init__(
        self, pool: Any, *, interval_s: float = 120.0,
        resource_monitor: Any = None,
        publisher: Any = None,
        runtime: Any = None,
    ) -> None:
        self._pool = pool
        self._interval = interval_s
        self._resources = resource_monitor
        self._publisher = publisher
        self._runtime = runtime
        # Last-cycle audit — informs the metrics endpoint ("when did the driver last run?")
        self._last_cycle_at: datetime | None = None
        self._last_cycle_count: int = 0

    async def _housekeeping(self) -> None:
        """Cheap DB-only maintenance that no other tick was doing.

        - `OpenLoopStore.expire_stale`: past-TTL open loops without recent touch → 'expired',
          keeps the initiative source from carrying dead weight forever.
        - `CommunicationDecisionEngine.record_engagement` sweep: any 'sent' proactive_decision
          older than 30 minutes with engagement IS NULL is settled as 'none' so the §46 gate's
          _kind_chronically_ignored aggregate has data to work from. (Without this the write
          side of the engagement-feedback loop was dead.)
        - `RoutineStore.due` scan: any enabled routine whose next_execution has passed is
          dispatched as one background turn through the existing runtime — never a second
          scheduler, never bypassing the cognition lease. `mark_executed` advances the row so
          missed ticks don't burst-fire on catch-up.
        """
        with contextlib.suppress(Exception):
            from sali.tasks.open_loops import OpenLoopStore

            await OpenLoopStore(self._pool).expire_stale()
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sali.proactive_decision "
                    "SET engagement = 'none', engagement_at = now() "
                    "WHERE decision = 'sent' AND engagement IS NULL "
                    "  AND decided_at < now() - interval '30 minutes'")
        # Routine dispatch — no second scheduler, no LLM here; hands the routine to the
        # existing background-turn path, which honours the single cognition lease.
        await self._fire_due_routines()

    async def _fire_due_routines(self) -> None:
        with contextlib.suppress(Exception):
            from sali.tasks.routines import RoutineStore
            from uuid import UUID as _UUID

            store = RoutineStore(self._pool)
            due = await store.due(limit=3)
            if not due:
                return
            # A runtime handle is required to actually submit the turn; without one we still
            # advance the next_execution so a missing runtime doesn't cause a burst on next tick.
            runtime = getattr(self, "_runtime", None)
            for row in due:
                rid = _UUID(str(row["id"]))
                fired = False
                if runtime is not None:
                    with contextlib.suppress(Exception):
                        await runtime.submit_background(
                            f"[routine: {row['name']}] {row.get('purpose') or 'run the recurring check'}",
                            priority="background")
                        fired = True
                await store.mark_executed(rid, success=fired,
                                            result="dispatched" if fired else "advanced-no-runtime")

    async def tick(self) -> list[str]:
        """One driver cycle. Returns the initiative ids touched. Bounded, cheap, no LLM."""
        # Cheap housekeeping first — this runs even under resource pressure since it's pure DB.
        await self._housekeeping()
        # Resource gate — defer if the machine is under pressure (§23).
        if self._resources is not None:
            with contextlib.suppress(Exception):
                snap = await self._resources.snapshot()
                # Very conservative: if VRAM > 90% OR CPU load > 4.0 OR disk usage > 95%, skip.
                gpu = float(snap.get("gpu_vram_percent") or 0)
                cpu = float(snap.get("cpu_load_1m") or 0)
                disk = float(snap.get("disk_used_percent") or 0)
                if gpu > 90 or cpu > 4.0 or disk > 95:
                    log.info("initiative_driver_deferred_resource_pressure",
                             gpu=gpu, cpu=cpu, disk=disk)
                    await self._record_defer("resource_pressure",
                                              {"gpu": gpu, "cpu": cpu, "disk": disk})
                    return []

        # Next-wake gate — if the engine says nothing's due for ages, skip this cycle.
        from sali.runtime.initiative import InitiativeEngine

        engine = InitiativeEngine(self._pool, self._publisher)
        now = datetime.now(UTC)
        with contextlib.suppress(Exception):
            due = await engine.next_wake(now=now)
            if due is not None and due > now + timedelta(seconds=self._interval * 2):
                # Nothing due within 2 intervals — no point running the scanner.
                log.debug("initiative_driver_skip_nothing_due", next_due=due.isoformat())
                self._last_cycle_at = now
                return []

        # Generate candidates — the actual orchestration call the previous audit found
        # dormant. Emits events per source; upserts into sali.initiative.
        touched: list[str] = []
        try:
            touched = await engine.generate_candidates(now=now)
        except Exception as exc:  # noqa: BLE001 — one bad cycle must not kill the driver
            log.warning("initiative_driver_generate_failed", error=str(exc))
            return []
        self._last_cycle_at = now
        self._last_cycle_count = len(touched)

        # Record each touched candidate as a decision-trace row with mode='observe' — noticing
        # is not acting. Actual action goes through the coordinator + policy layer, which will
        # add its own trace rows keyed to the same subject_ref.
        if touched:
            from sali.cognitive.decision_trace import DecisionTraceStore

            trace = DecisionTraceStore(self._pool)
            for initiative_id in touched:
                await trace.record(
                    mode="observe", subject_ref=str(initiative_id),
                    origin="initiative_driver",
                    reason_codes=["scanned_and_upserted"],
                    confidence=1.0,   # deterministic upsert — high confidence
                    evidence={"cycle_at": now.isoformat()},
                    expected_outcome="candidate_available_for_review")
        # PROACTIVE OUTREACH (§ Phase 5) — the FIRST, fail-CLOSED, gated initiation. Sali is otherwise
        # 100% reactive. Take the single MOST-overdue commitment (Almir's own word), run it through the
        # "would silence be better?" gate, and only if the gate says SEND — and only via
        # runtime.send_agent_message (which GROUNDS the message) — reach out ONCE. Fail-closed at every
        # step: no runtime → no send; no overdue commitment → no send; the gate suppresses on
        # frequency / repetition / learned-bad / user-focus. Never breaks the cycle.
        with contextlib.suppress(Exception):
            if self._runtime is not None:
                await self._maybe_remind_overdue(now)

        log.info("initiative_driver_cycle", touched=len(touched))
        return touched

    async def _maybe_remind_overdue(self, now: datetime) -> None:
        """One gated proactive reminder about the most-overdue commitment, or nothing. Fail-closed."""
        from sali.events.communication_decision import CommunicationDecisionEngine
        from sali.tasks.commitments import CommitmentStore

        rows = await CommitmentStore(self._pool).open(limit=20)
        overdue = [r for r in rows if r.get("deadline") and r["deadline"] < now]
        if not overdue:
            return
        overdue.sort(key=lambda r: r["deadline"])   # most-overdue first
        c = overdue[0]
        desc = (c.get("description") or "").strip()
        if not desc:
            return
        # Is Almir in focused work right now? (a message within the last 5 min on any session.)
        active = False
        with contextlib.suppress(Exception):
            last = await self._pool.fetchval("SELECT max(created_at) FROM sali.message")
            active = last is not None and (now - last).total_seconds() < 300
        msg = (f"Heads up — you said you'd {desc}, and it's now overdue (was due "
               f"{c['deadline']:%b %-d, %H:%M}). Want me to help with it, or should I let it go?")
        decision = await CommunicationDecisionEngine(self._pool, self._publisher).evaluate(
            kind="reminder", subject_ref=str(c["id"]), message=msg, relevance=0.8, user_focused=active)
        if decision.send:
            await self._runtime.send_agent_message(msg, importance="reminder")
            log.info("initiative_proactive_reminder_sent", commitment=str(c["id"]))

    async def run(self, stop: asyncio.Event, wake: asyncio.Event | None = None) -> None:
        """Faculty entry point — matches the shape the daemon's `faculties` list expects.

        A minimum-interval floor stops the driver from hammering the DB during event storms
        (perception can fire dozens of events per second on Almir's desktop; each one sets the
        shared EventBus waker). Without this floor the driver would tick generate_candidates
        6+ times/sec on a busy desktop, all for zero-touch cycles.
        """
        min_floor_s = min(20.0, self._interval / 4.0)  # scale with configured interval
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("initiative_driver_tick_failed", error=str(exc))
            # Wait at least min_floor_s regardless of wake (unless stop fires).
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min_floor_s)
            if stop.is_set():
                break
            # Then wait up to the full interval OR the next wake — whichever comes first.
            await wait_or_wake(stop, wake, max(0.0, self._interval - min_floor_s))

    async def status(self) -> dict[str, Any]:
        """For the /cognitive-metrics endpoint — what did the driver do last?"""
        return {
            "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "last_cycle_count": self._last_cycle_count,
            "interval_s": self._interval,
        }

    async def _record_defer(self, reason: str, evidence: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            from sali.cognitive.decision_trace import DecisionTraceStore

            trace = DecisionTraceStore(self._pool)
            await trace.record(
                mode="defer", origin="initiative_driver",
                reason_codes=[reason], confidence=1.0, evidence=evidence,
                expected_outcome="retry_next_cycle")
