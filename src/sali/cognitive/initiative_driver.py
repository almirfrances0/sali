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

**Not another mind**: no separate reasoning stream, no cognition lock, no second agent.
Reuses the existing InitiativeEngine, ResourceMonitor, EventBus, DecisionTraceStore.

The one qualification to that, added deliberately: when this driver has already DECIDED to say
something, it asks the model for the WORDING (`cognitive.voice`). Deciding stays here and stays
deterministic — that is what keeps Sali from being annoying. But a person doesn't recite the same
sentence every time, and a message assembled by an f-string reads as machinery no matter how good
the judgement behind it was. The composer refuses to run while Almir is being served, and falls back
to the deterministic text on any doubt, so this is not a reasoning stream — it is a phrasing call.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sali.cognitive import voice
from sali.events.bus import wait_or_wake
from sali.obs.log import get_logger

log = get_logger("sali.cognitive.initiative_driver")


def _gap(delta: timedelta) -> str:
    """How late, in the units a person would actually use out loud."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 3600:
        n = max(1, secs // 60)
        return f"{n} minute" + ("s" if n != 1 else "")
    if secs < 86_400:
        n = secs // 3600
        return f"{n} hour" + ("s" if n != 1 else "")
    n = secs // 86_400
    return f"{n} day" + ("s" if n != 1 else "")


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
        # DELETED: the blanket "assume ignored after 30 minutes" sweep.
        #
        # It stamped engagement='none' on EVERY sent message older than half an hour, and nothing
        # anywhere writes 'replied' or 'read' — so silence was the only signal the system could ever
        # record. _kind_chronically_ignored mutes a kind+subject for 7 days once the last 5 scored
        # messages are all 'none', and the reminder window is itself 30 minutes: five messages, about
        # 2.5 hours, and Sali permanently stops raising that subject EVEN IF Almir answered every one.
        # A voice that can only ever learn "be quieter" is worse than no voice.
        #
        # Leaving engagement NULL is the correct "unknown": _kind_chronically_ignored already filters
        # on `engagement IS NOT NULL`, so an unscored message simply teaches nothing. Only genuinely
        # OBSERVED engagement (Almir replied, or the app reported the message was read) should ever
        # count — and never-answered-but-seen is his prerogative, not a snub.
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
                        from sali.runtime.session import background_session_id
                        # Sali's OWN scheduled work belongs in his own conversation. Without this it
                        # defaults to the owner's session and a routine check reads, in Almir's chat,
                        # as Sali talking to himself unprompted.
                        await runtime.submit_background(
                            f"[routine: {row['name']}] {row.get('purpose') or 'run the recurring check'}",
                            session_id=background_session_id(), priority="background")
                        fired = True
                await store.mark_executed(rid, success=fired,
                                            result="dispatched" if fired else "advanced-no-runtime")

    async def _dispatch_curiosity(self) -> str | None:
        """Spend ONE free-time slot actually studying the most-wanted curiosity.

        This is the step that turns "Sali noticed something worth learning" into Sali learning it. The
        driver already generated curiosity candidates and already held a runtime handle — it simply never
        dispatched them, so a topic could be raised, scored and ranked forever without a single session
        ever being spent on it. That is why "go learn X" appeared to do nothing.

        Pacing is deliberate and uses columns the initiative table already has for this: ONE topic per
        tick, and `next_attempt` keeps the same topic from being restudied for 6 hours. Learning is meant
        to accumulate across days, not burn an afternoon of GPU. The free-time contract comes from
        `submit_background`, which defers while Almir is waiting and cancels mid-thought at his first
        keystroke — so a learning session can never compete with him."""
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT i.id AS iid, c.id AS cid, c.subject, c.statement, c.current_understanding "
                "FROM sali.initiative i "
                "  JOIN sali.curiosity c ON c.id::text = i.subject_ref "
                "WHERE i.source = 'curiosity' "
                "  AND i.status NOT IN ('completed','dismissed','expired') "
                "  AND c.status IN ('open','investigating') "
                "  AND (i.next_attempt IS NULL OR i.next_attempt <= now()) "
                "ORDER BY i.priority_score DESC, i.created_at LIMIT 1")
            if row is None:
                return None
            # Reserve BEFORE dispatching: if the turn is deferred or wedged we still hold the back-off,
            # so a busy evening can never turn into the same topic being retried every 120 seconds.
            await conn.execute(
                "UPDATE sali.initiative SET attempts = attempts + 1, "
                "  next_attempt = now() + interval '6 hours', updated_at = now() WHERE id = $1",
                row["iid"])

        subject = str(row["subject"])
        understood = str(row["current_understanding"] or "").strip()
        prompt = (
            f"[learning session] You have free time, so study ONE topic a little further — a single "
            f"useful step, not the whole subject.\n\n"
            f"Topic: {subject}\n"
            f"Why it came up: {row['statement']}\n"
            f"What you already understand: {understood or '(nothing recorded yet)'}\n\n"
            "Search the web for something specific you do NOT already know about it, read one good "
            "source properly, then record what you learned with the learn_curiosity tool, citing the "
            "source url. If you find nothing genuinely new, record nothing and say so."
        )
        with contextlib.suppress(Exception):
            from sali.runtime.session import background_session_id
            # A learning session is Sali studying on his own time. It is not a reply to Almir and must
            # not land in his transcript as one.
            await runtime.submit_background(prompt, session_id=background_session_id(),
                                            priority="background")
            log.info("curiosity_session_dispatched", subject=subject[:80])
            return str(row["cid"])
        return None

    async def tick(self) -> list[str]:
        """One driver cycle. Returns the initiative ids touched. Bounded, cheap, no LLM."""
        # Cheap housekeeping first — this runs even under resource pressure since it's pure DB.
        await self._housekeeping()

        # SPEAKING TO ALMIR COMES FIRST — before every early return below.
        #
        # This block used to sit at the BOTTOM of the tick, under the resource gate, under the
        # "nothing due within two intervals" next-wake gate, and under `generate_candidates`
        # returning [] on error. Those all concern the initiative SCANNER; whether Sali has
        # something worth telling Almir is a different question entirely, and wiring the second
        # behind the first meant he went quiet with a fresh discovery sitting in the table and every
        # communication gate saying send — simply because the scanner had nothing queued.
        #
        # Safe here: two indexed reads plus the gate, fail-closed at every step, and the composer
        # itself declines while Almir is being served.
        outreach_now = datetime.now(UTC)
        with contextlib.suppress(Exception):
            if self._runtime is not None:
                await self._maybe_remind_overdue(outreach_now)
                # Overdue commitments are the URGENT reason to speak; something he learned is the
                # human one. Both pass the same gate, so neither can become noise.
                with contextlib.suppress(Exception):
                    await self._maybe_share_discovery(outreach_now)
        # Resource gate — defer if the machine is under pressure (§23).
        if self._resources is not None:
            with contextlib.suppress(Exception):
                snap = await self._resources.snapshot()
                # This gate was DEAD: it read "gpu_vram_percent" / "cpu_load_1m" / "disk_used_percent",
                # but snapshot() returns {"preserve", "reading", "state"} — so all three were None, all
                # three became 0.0, and the branch could never be true. Use the monitor's OWN verdict
                # instead of re-deriving thresholds that can drift out of sync a second time.
                state = str(getattr(snap.get("state"), "value", snap.get("state")) or "").lower()
                if state in ("high", "critical", "emergency") or bool(snap.get("preserve")):
                    log.info("initiative_driver_deferred_resource_pressure", state=state)
                    await self._record_defer("resource_pressure", {"state": state})
                    return []

        # Next-wake gate — if the engine says nothing's due for ages, skip this cycle.
        from sali.runtime.initiative import InitiativeEngine

        engine = InitiativeEngine(self._pool, self._publisher)
        now = datetime.now(UTC)
        with contextlib.suppress(Exception):
            # NOT include_initiative: the driver must never put itself to sleep with the back-off it
            # set on its own dispatched curiosity (that darkened the whole scanner for 6h).
            due = await engine.next_wake(now=now, include_initiative=False)
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

        # Noticing is not learning. Spend one free-time slot on the top curiosity.
        with contextlib.suppress(Exception):
            await self._dispatch_curiosity()
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
        log.info("initiative_driver_cycle", touched=len(touched))
        return touched

    async def _almir_is_here(self, now: datetime) -> bool:
        """Is this a bad moment to speak — is Almir mid-action right now?

        The real signal is the X server's idle counter, which knows to the millisecond when Almir
        last touched the keyboard. Someone sitting next to you doesn't talk over your typing and
        doesn't talk to an empty chair; they speak in the gap. So this is True only while his hands
        are actually on the machine, and a pause of a few seconds — or him being away entirely — is
        a fine time to say something he'll read when he looks.

        The old answer was "did a user message arrive in the last five minutes", which is a proxy for
        the wrong thing: it stayed True for five minutes after he walked away mid-conversation, and
        was False while he sat there working in silence. It survives only as the fallback for when
        there is no display to ask.
        """
        with contextlib.suppress(Exception):
            from sali.perception import presence

            st = presence.state()
            if st != "unknown":
                return st == "working"
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                last = await conn.fetchval(
                    "SELECT max(created_at) FROM sali.message WHERE role = 'user'")
            return last is not None and (now - last).total_seconds() < 300
        return False

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
        # ALMIR's last message, not Sali's own. `sali.message` now carries role='agent' rows too —
        # his unprompted messages are persisted so they survive a reopen — so an unfiltered max()
        # meant Sali read HIS OWN message as Almir being active, and then deferred to it for five
        # minutes. He was suppressing himself with his own voice.
        active = await self._almir_is_here(now)

        gate = CommunicationDecisionEngine(self._pool, self._publisher)
        # Ask before writing. Composing costs a real generation on the one GPU this machine has;
        # spending it on a message that is about to be suppressed is how a background faculty starts
        # competing with Almir. `evaluate` below is still the authority and still gets the last word.
        if not await gate.would_send(kind="reminder", subject_ref=str(c["id"]),
                                     relevance=0.8, user_focused=active):
            return

        # In ALMIR's timezone. The host clock is hours away from his, so a deadline formatted raw
        # told him the wrong time — about his own commitment.
        due = c["deadline"]
        with contextlib.suppress(Exception):
            due = self._runtime.temporal.local(c["deadline"])
        fallback = (f"Heads up — you said you'd {desc}, and it's now overdue (was due "
                    f"{due:%b %-d, %H:%M}). Want me to help with it, or should I let it go?")
        msg = await voice.compose(
            situation=("Almir told you he would do something, and the time he gave has passed. "
                       "Mention it once, the way you'd remind someone you like — it is completely "
                       "fine if he has changed his mind about it."),
            facts={"what he said he'd do": desc,
                   "when it was due": f"{due:%b %-d at %H:%M}",
                   "overdue by": _gap(now - c["deadline"]),
                   "the next step he named": c.get("next_action"),
                   "how many other things of his are also overdue": (len(overdue) - 1) or None,
                   "what you have done about it": "nothing — you haven't started it and won't unless "
                                                 "he asks"},
            fallback=fallback)

        decision = await gate.evaluate(
            kind="reminder", subject_ref=str(c["id"]), message=msg, relevance=0.8,
            user_focused=active)
        if decision.send:
            await self._runtime.send_agent_message(msg, importance="reminder",
                                                   decision_id=decision.id)
            log.info("initiative_proactive_reminder_sent", commitment=str(c["id"]))

    async def _maybe_share_discovery(self, now: datetime) -> None:
        """Tell Almir something Sali learned on his own — the human half of learning.

        Learning that is never mentioned is just a private hobby. This is the outreach path with real
        SUBSTANCE behind it: it only speaks when a curiosity has gained a discovery Sali has not already
        told him about, so there is always something actually new to say.

        Two things keep it from becoming a robot. (1) It goes through the SAME CommunicationDecisionEngine
        gate as every other unsolicited message — rate window, per-subject repetition, relevance, and
        deference while Almir is in focused work — and sends only if the gate says send. (2) The NOT EXISTS
        below means one discovery is mentioned at most once: he cannot re-tell the same thing tomorrow.
        Phrased as sharing, not as a question demanding an answer — Almir replying is his choice."""
        from sali.events.communication_decision import CommunicationDecisionEngine

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                # The whole substance of the curiosity, not just its title: why he got interested,
                # what he understood before, how long he has been circling it. A sentence written
                # from four columns can only ever be a template; this is what makes it his.
                "SELECT c.id, c.subject, c.statement, c.why_it_matters, c.current_understanding, "
                "       c.times_encountered, c.discoveries, c.updated_at "
                "FROM sali.curiosity c "
                "WHERE c.status IN ('open','investigating') "
                "  AND jsonb_array_length(c.discoveries) > 0 "
                # Only if something is NEW since the last time he mentioned this topic.
                "  AND NOT EXISTS (SELECT 1 FROM sali.proactive_decision p "
                "                  WHERE p.kind = 'discovery' AND p.subject_ref = c.id::text "
                "                    AND p.decision = 'sent' AND p.decided_at >= c.updated_at) "
                "ORDER BY c.updated_at DESC LIMIT 1")
        if row is None:
            return
        discoveries = row["discoveries"] or []
        if not discoveries:
            return
        latest = discoveries[-1] if isinstance(discoveries, list) else {}
        note = str((latest or {}).get("note") or "").strip()
        if not note:
            return
        subject = str(row["subject"]).replace("-", " ")

        # Is Almir mid-conversation right now? Then this can wait — it is not urgent. (Filtered to
        # HIS messages: Sali's own persisted messages used to count as Almir being present.)
        active = await self._almir_is_here(now)

        gate = CommunicationDecisionEngine(self._pool, self._publisher)
        if not await gate.would_send(kind="discovery", subject_ref=str(row["id"]),
                                     relevance=0.6, user_focused=active):
            return

        fallback = (f"I've been reading about {subject} in my own time, and found something worth "
                    f"passing on: {note}")
        msg = await voice.compose(
            situation=("You went and read about something on your own time, because you were curious "
                       "— nobody asked you to. You found something worth telling Almir. Share it the "
                       "way you'd mention an interesting thing to someone; you're not reporting, and "
                       "you're not asking him for anything."),
            facts={"what you were curious about": subject,
                   "why it caught your interest": row["why_it_matters"] or row["statement"],
                   # current_understanding is updated BY learn(), so it is what he thinks NOW —
                   # labelling it "before" invited him to narrate a change he was never told about,
                   # which is exactly the drift the grounding check then had to catch.
                   "what you understand now": row["current_understanding"],
                   "what you just found out": note,
                   "how many times you've come back to this": row["times_encountered"],
                   # Sharing what he READ must never read as reporting what he DID. The grounding
                   # pass on this channel checks state and capability claims, not action claims, so
                   # "I fixed it" would sail straight through — state the truth as a fact instead of
                   # hoping the wording avoids it.
                   "what you did about it": "nothing — you read about it, you have not changed "
                                            "anything on this machine"},
            fallback=fallback)

        decision = await gate.evaluate(
            kind="discovery", subject_ref=str(row["id"]), message=msg,
            relevance=0.6, user_focused=active)
        if decision.send and self._runtime is not None:
            await self._runtime.send_agent_message(msg, importance="update",
                                                   decision_id=decision.id)
            log.info("proactive_discovery_shared", subject=subject[:60])

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
