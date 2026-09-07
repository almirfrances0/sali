"""The Proactive loop (spec §16/§74–78) — Sali speaks up on its own, without being asked.

The Attention Engine tags each observation with an action; the ones it marks `notify` (critical:
a service failed, a disk filling) or `investigate` (a new listening service) are exactly the things
worth telling Almir promptly. This loop watches for those and delivers a short message into his chat,
then records that it announced them so it never repeats itself.

The DECISION is still fully deterministic — which observations qualify, and whether the gate lets one
through, involve no model at all (§14/§79), which is why this can tick every 20s. What changed is the
WORDING: once the gate has agreed the message should exist, Sali writes the sentence himself. A fixed
template made every observation arrive in the same shape no matter what had happened, which reads as
machinery rather than as someone telling you something. The composer declines whenever Almir is being
served and falls back to the template on any doubt, so the old behaviour is the floor, not the norm.

Crucially it only INFORMS; it never acts on the machine (§46/§78): "a new service is listening on
port X — I haven't touched it." Attention already gates the volume to the genuinely-notable, so this
respects "don't annoy Almir" (§15) by construction. On startup it sets its watermark to the current
tip, so a restart never dumps a backlog of stale notifications.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sali.cognitive import voice
from sali.events.bus import wait_or_wake
from sali.obs.log import get_logger
from sali.tools.builtins.notify_tool import _desktop_env, _notify_send

log = get_logger("sali.events.proactive")

# Attention verdicts worth surfacing. 'record' is included because the attention tiering maps
# IMPORTANT observations to RECORD, so filtering to notify/investigate read a permanently EMPTY
# stream — live, every desktop.observed row carried action='record'. The churn Almir objected to is
# excluded by TIER instead (see the tick query): important/critical only, never the routine noise.
_ACTIONS = ("notify", "investigate", "record")
_TIERS = ("important", "critical")


def compose(payload: dict[str, Any]) -> tuple[str, str]:
    """A short (title, body) for a surfaced observation — deterministic, in Sali's voice, and always
    making clear Sali has NOT acted (§78)."""
    kind = str(payload.get("kind", ""))
    summary = str(payload.get("summary", "something changed"))
    sample = str((payload.get("detail") or {}).get("sample", ""))
    if kind == "service_failed":
        return "Sali — service failed", f"{sample or summary} failed. It was running before; I haven't touched it."
    if kind == "disk_pressure":
        return "Sali — disk almost full", f"{sample or 'A disk'} is nearly full."
    if kind == "port_opened":
        return "Sali — new service", f"A new service is listening on {sample or 'a port'}. It wasn't there before — I haven't touched it."
    return "Sali", summary


class ProactiveLoop:
    def __init__(self, pool: Any, *, interval: float = 20.0, max_per_tick: int = 3,
                 runtime: Any = None) -> None:
        self._pool = pool
        self._interval = interval
        self._max = max_per_tick
        # A runtime handle turns this from a desktop popup into a message in Almir's chat — the only
        # channel he actually reads. Optional so tests and one-shots can still construct it bare.
        self._runtime = runtime
        self._watermark = -1  # event seq; -1 until initialised to the current tip

    async def _ensure_watermark(self, conn: Any) -> None:
        if self._watermark < 0:  # first run: start from the tip so no stale backlog is announced
            self._watermark = int(await conn.fetchval("SELECT COALESCE(max(seq), 0) FROM event") or 0)

    async def tick(self, *, deliver: bool = True) -> list[str]:
        """Announce any new notify/investigate observations. Every candidate flows through the
        CommunicationDecisionEngine BEFORE delivery — suppressed candidates stay silent (with
        an audit-trail row explaining why). Returns the bodies actually delivered."""
        # Local import — CommunicationDecisionEngine lives in the same module tree; keeping the
        # import lazy avoids a circular-import risk in tests that don't need the gate.
        from sali.events.communication_decision import CommunicationDecisionEngine

        async with self._pool.acquire() as conn:
            await self._ensure_watermark(conn)
            rows = await conn.fetch(
                # notify/investigate are ALREADY explicit "surface this" verdicts — they carry no
                # tier requirement. The tier gate applies only to 'record', which had to be admitted
                # (attention maps IMPORTANT observations to RECORD, so the old filter read an empty
                # stream) but is also where the port-churn noise lives.
                "SELECT seq, payload FROM event WHERE event_type='desktop.observed' AND seq > $1 "
                "AND (payload->>'action' IN ('notify','investigate') "
                "     OR (payload->>'action' = 'record' AND payload->>'tier' = ANY($2))) "
                "ORDER BY seq LIMIT $3",
                self._watermark, list(_TIERS), self._max)

        gate = CommunicationDecisionEngine(self._pool)
        delivered: list[str] = []
        env = _desktop_env()
        for r in rows:
            title, body = compose(r["payload"])
            payload = r["payload"] or {}
            # Map attention action → decision kind so learning can aggregate over meaningful
            # groupings (a 'notify' from a service_failed vs a 'notify' from disk_pressure are
            # different KINDS as far as the audit trail is concerned; both are still 'concern').
            kind_map = {"service_failed": "concern", "disk_pressure": "concern",
                        "port_opened": "observation"}
            obs_kind = str(payload.get("kind", ""))
            kind = kind_map.get(obs_kind, "observation")
            detail = payload.get("detail") or {}
            # A STABLE identity for the subject. This used to be `detail.sample` — a per-task file
            # path, different on every single observation — so `too_repetitive` could never once
            # fire: the same thing recurring forever always looked like a brand-new subject, and only
            # the 2h per-kind window held it back. What actually repeats is the KIND of thing and
            # where it happened.
            subject_ref = ":".join(p for p in (obs_kind or "observation",
                                               str(detail.get("where") or "")) if p)

            if not await gate.would_send(kind=kind, subject_ref=subject_ref, relevance=0.7,
                                         user_focused=False):
                continue   # the watermark already moved: noticed, considered, deliberately unsaid

            # He noticed this himself, so let him say it himself. The "I haven't touched it" clause
            # is passed as a FACT rather than a phrase to copy, because it is a truth claim (§78) —
            # Sali observing the machine must never read as Sali having acted on it.
            body = await voice.compose(
                situation=("You noticed something change on this machine while watching it. Tell "
                           "Almir what you saw, plainly. You are telling him, not asking him."),
                facts={"what happened": payload.get("summary"),
                       "kind of thing": obs_kind or None,
                       "where": detail.get("where") or detail.get("sample") or None,
                       "how many times seen": payload.get("count"),
                       "how serious": payload.get("tier"),
                       "did you do anything about it": "no — you only watched; you have not touched "
                                                       "or changed anything"},
                fallback=body)

            decision = await gate.evaluate(
                kind=kind, subject_ref=subject_ref, message=body,
                relevance=0.7,  # attention-flagged events are already high-relevance signals
                user_focused=False)   # attention runs even when Almir is offline; not fg-aware
            # Advance the watermark regardless of the decision — a suppressed observation
            # has already been NOTICED; not re-considering it is the whole point of the gate.
            self._watermark = max(self._watermark, int(r["seq"]))
            if not decision.send:
                continue
            if deliver:
                # Chat first — a libnotify popup at DISPLAY=:0 reaches nobody on a headless box, which
                # is why this faculty could appear to "work" while Almir never saw a thing. The desktop
                # popup stays only as a fallback when no runtime is wired.
                spoke = False
                if self._runtime is not None:
                    with contextlib.suppress(Exception):
                        await self._runtime.send_agent_message(
                            body, importance="update", decision_id=decision.id)
                        spoke = True
                if not spoke:
                    with contextlib.suppress(Exception):
                        _notify_send(title, body, env)
            async with self._pool.acquire() as conn:  # audit trail — what Sali told Almir, and when
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('sali.proactive','desktop',$1)", {"source_seq": r["seq"], "message": body})
            delivered.append(body)
        if delivered:
            log.info("proactive", count=len(delivered))
        return delivered

    async def run(self, stop: asyncio.Event, wake: asyncio.Event | None = None) -> None:
        while not stop.is_set():
            try:
                # Deliver for real now. This was deliver=False because delivery MEANT a desktop
                # popup, and Almir rightly found those noisy on a box where ports churn constantly —
                # but that muted the entire faculty, so he never heard anything Sali noticed about
                # his own machine. The noise is now handled where it belongs: only important/critical
                # tiers are read at all, and every candidate still passes the CommunicationDecisionEngine
                # (rate window, repetition, relevance, focus) before a word reaches him.
                await self.tick(deliver=True)
            except Exception as exc:  # noqa: BLE001 - never let a bad cycle kill the faculty
                log.warning("proactive tick failed: %s", exc)
            await wait_or_wake(stop, wake, self._interval)  # push when subscribed, else heartbeat
