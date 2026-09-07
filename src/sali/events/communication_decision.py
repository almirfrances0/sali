"""CommunicationDecisionEngine — the "would silence be better?" gate (§10, §11, §45, §46).

Before Sali sends an unsolicited proactive message, this engine evaluates the decision. The
default is silence. A message is sent only when a positive reason clears every suppression
check.

Suppression checks (any hit → suppress):

* **too_frequent** — a message of the same KIND was sent within the past frequency window.
* **too_repetitive** — the same subject_ref was messaged about within a recency window.
* **kind_learned_bad** — historical engagement for this KIND is chronically low (§46 learn
  when not to speak). Aggregated from `sali.proactive_decision.engagement` when the row is
  scored later.
* **low_relevance** — the payload flag says "informational, no action needed"; combined with
  no user-in-flight-work signal, silence wins.
* **user_focused** — a chat/task is currently active on the same session; interrupting
  focused work with a low-priority proactive is worse than waiting.

Every decision (sent OR suppressed) is written to `sali.proactive_decision` with reason codes,
so aggregate analysis over time can learn which KINDs Almir cares about and which he ignores.
The store is the audit trail; the aggregator is stateless (queries the audit).

Not an authority to send — the caller still owns the actual delivery. This engine returns a
`Decision` object; if `decision.send is False`, the caller must not deliver.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.events.communication_decision")


# Frequency windows per KIND (seconds since the last one). A message of the same KIND landing
# within its window is 'too_frequent'.
_KIND_WINDOWS: dict[str, int] = {
    "greeting":         12 * 3600,    # once every 12h
    "social_checkin":   6 * 3600,     # every 6h max
    "reminder":         30 * 60,      # 30 min between the same reminder subject
    "follow_up":        3 * 3600,     # 3h between follow-ups on the same subject
    "observation":      2 * 3600,     # environmental observations, 2h
    "discovery":        60 * 60,      # 1h between discoveries
    "opportunity":      6 * 3600,     # opportunities aren't urgent
    "concern":          15 * 60,      # concerns can repeat sooner if the state persists
}
# How many unanswered messages before he waits for Almir instead of adding another. Two is the
# point at which a person notices they are talking into silence.
_MAX_UNANSWERED = 2
_DEFAULT_WINDOW_S = 2 * 3600


# How many recent CONSECUTIVE 'engagement=none' scores of the same KIND to trigger
# 'kind_learned_bad'. Bounded so a temporary quiet period doesn't permanently silence a kind.
_MAX_CONSECUTIVE_IGNORED = 5


@dataclass(slots=True)
class Decision:
    send: bool
    kind: str
    subject_ref: str | None
    reason_codes: list[str] = field(default_factory=list)
    # A short human-readable note for the audit trail — useful in diagnostics.
    note: str = ""
    # The durable row id, set by _record. Without it the whole feedback half is impossible: the
    # engagement endpoint takes a decision id, and neither the caller, the event, nor the client ever
    # possessed one — so "Almir read this but chose not to answer" could never be reported.
    id: str | None = None


class CommunicationDecisionEngine:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def evaluate(
        self, *, kind: str, subject_ref: str | None = None,
        message: str = "",
        relevance: float = 0.5,
        user_focused: bool = False,
        ignore_frequency: bool = False,
    ) -> Decision:
        """Evaluate whether to send. Returns a Decision + audit-friendly reason codes.

        The caller (proactive.py, or any faculty about to send an unsolicited message) MUST
        respect `decision.send`. Every evaluation (send or suppress) writes to
        `sali.proactive_decision` for later aggregation.
        """
        reasons = await self._assess(kind=kind, subject_ref=subject_ref, relevance=relevance,
                                     user_focused=user_focused, ignore_frequency=ignore_frequency)
        send = len(reasons) == 0
        decision = Decision(send=send, kind=kind, subject_ref=subject_ref,
                            reason_codes=reasons,
                            note=("send" if send
                                  else "suppressed: " + ",".join(reasons)))

        # Audit the decision — even suppressions are recorded so the aggregator can learn.
        await self._record(decision, message=message)
        # An event so observability + iOS can render "considered vs sent" counts.
        await self._emit(decision)
        return decision

    async def would_send(self, *, kind: str, subject_ref: str | None = None,
                         relevance: float = 0.5, user_focused: bool = False,
                         ignore_frequency: bool = False) -> bool:
        """A no-commit preview: would `evaluate` say send, right now?

        Records nothing, emits nothing, teaches nothing. It exists so a caller can avoid expensive
        work — composing a sentence with the model — for a message the gates are about to suppress.
        It is NOT a substitute for `evaluate`: state can move between the two, and only `evaluate`
        writes the ledger row that the frequency window and the engagement learner read. Ask this
        first, then call `evaluate` last and obey it.
        """
        return not await self._assess(kind=kind, subject_ref=subject_ref, relevance=relevance,
                                      user_focused=user_focused, ignore_frequency=ignore_frequency)

    async def _assess(
        self, *, kind: str, subject_ref: str | None, relevance: float,
        user_focused: bool, ignore_frequency: bool,
    ) -> list[str]:
        """The five gates, as PURE READS — every reason this message should not be sent.

        Split out of `evaluate` so that deciding and committing are separable. Nothing in here
        writes; the message text is deliberately not a parameter, because no gate has ever consulted
        it and none should start: restraint is about WHO/WHAT/WHEN, never about wording.
        """
        reasons: list[str] = []
        window_s = _KIND_WINDOWS.get(kind, _DEFAULT_WINDOW_S)

        # 1) too_frequent: same kind sent within the frequency window
        if not ignore_frequency and await self._sent_within(kind, seconds=window_s,
                                                            subject_ref=None):
            reasons.append("too_frequent")

        # 2) too_repetitive: same subject_ref within a shorter recency window
        if subject_ref and await self._sent_within(kind, seconds=min(window_s, 3600),
                                                    subject_ref=subject_ref):
            reasons.append("too_repetitive")

        # 3) kind_learned_bad: N consecutive 'engagement=none' scores for this kind
        # AND this specific subject_ref (per-subject, time-bounded). Ignoring 5 reminders about
        # commitment A must not silence a fresh reminder about commitment B.
        if await self._kind_chronically_ignored(kind, subject_ref=subject_ref):
            reasons.append("kind_learned_bad")

        # 4) low_relevance: below 0.3 relevance and no explicit signal Almir cares about it
        if relevance < 0.3:
            reasons.append("low_relevance")

        # 5) user_focused: the caller says Almir is currently in focused work. Only a 'concern'
        # (urgent, state-driven) overrides and interrupts. A reminder defers to focused work
        # regardless of relevance — the driver builds this signal precisely so a reminder does
        # not barge into a live conversation. Other ambient kinds still interrupt only when
        # genuinely high-relevance (>= 0.7), otherwise they too defer.
        if user_focused and kind not in ("concern",) and (kind == "reminder" or relevance < 0.7):
            reasons.append("user_focused")

        # 6) no_reply_yet: he has already been told things he hasn't come back on.
        #
        # A person who says two things and gets no reply stops volunteering a third. This is NOT the
        # mute rule — nothing is being learned or held against him, and 'read' is still not a snub.
        # It is simply that the conversation has not started, and the right move is to wait rather
        # than to keep filling it. It clears completely the moment Almir says anything at all, and a
        # genuine 'concern' (a service down, a disk filling) still comes through.
        if kind != "concern" and await self._unanswered_since_he_spoke() >= _MAX_UNANSWERED:
            reasons.append("no_reply_yet")

        return reasons

    async def _unanswered_since_he_spoke(self) -> int:
        """How many unprompted messages Sali has sent since Almir last said anything.

        Deliberately measured against Almir's own last message rather than a time window: two
        messages he never replied to mean the same thing whether they were minutes or hours apart,
        and the count resets the instant he speaks.
        """
        try:
            return int(await self._pool.fetchval(
                "SELECT count(*) FROM sali.proactive_decision "
                "WHERE decision = 'sent' AND decided_at > coalesce("
                "  (SELECT max(created_at) FROM sali.message WHERE role = 'user'), "
                "  now() - interval '24 hours')") or 0)
        except Exception:  # noqa: BLE001 - unknown means don't gag him
            return 0

    async def record_engagement(self, decision_id: str, *, engagement: str) -> None:
        """External signal (Almir read the notification, replied to a chat message, acted on
        an observation). Retroactively scores a `sent` decision so the learning aggregator can
        pick it up."""
        if engagement not in ("none", "read", "replied", "acted"):
            return
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sali.proactive_decision "
                    "SET engagement=$2, engagement_at=now() WHERE id=$1",
                    decision_id, engagement)

    # ── internals ─────────────────────────────────────────────────────────────────────────────

    async def _sent_within(self, kind: str, *, seconds: int,
                           subject_ref: str | None) -> bool:
        # Query wrapped so a missing audit table (fresh test DB, unapplied migration) degrades
        # gracefully to "no prior history" rather than crashing every proactive tick. In
        # production the migration runs at boot; the suppress is a safety net for the test path
        # and any transient DB error where safe-default = allow.
        try:
            async with self._pool.acquire() as conn:
                if subject_ref is None:
                    n = await conn.fetchval(
                        "SELECT count(*) FROM sali.proactive_decision "
                        "WHERE kind = $1 AND decision = 'sent' "
                        "  AND decided_at > now() - make_interval(secs => $2)",
                        kind, float(seconds))
                else:
                    n = await conn.fetchval(
                        "SELECT count(*) FROM sali.proactive_decision "
                        "WHERE kind = $1 AND subject_ref = $2 AND decision = 'sent' "
                        "  AND decided_at > now() - make_interval(secs => $3)",
                        kind, subject_ref, float(seconds))
            return int(n or 0) > 0
        except Exception:  # noqa: BLE001 — missing table / transient DB error → no history known
            return False

    async def _kind_chronically_ignored(self, kind: str,
                                        *, subject_ref: str | None = None) -> bool:
        """True when the last N SENT messages of this kind AND this subject_ref all have
        engagement=none, within the last 7 days. Requires at least N scored messages; a young
        subject (fewer scored) is NOT flagged. Per-subject + time-bounded so ignoring reminders
        about commitment A cannot go on to silence a different commitment B, and so a stale run
        of ignores eventually ages out. A None subject_ref never trips this (no subject to
        learn against).
        Wrapped for the same "missing table = no history" reason as `_sent_within`."""
        if subject_ref is None:
            return False
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT engagement FROM sali.proactive_decision "
                    "WHERE kind = $1 AND subject_ref = $2 "
                    "  AND decision = 'sent' AND engagement IS NOT NULL "
                    "  AND decided_at > now() - interval '7 days' "
                    "ORDER BY decided_at DESC LIMIT $3",
                    kind, subject_ref, _MAX_CONSECUTIVE_IGNORED)
        except Exception:  # noqa: BLE001
            return False
        if len(rows) < _MAX_CONSECUTIVE_IGNORED:
            return False
        return all(r["engagement"] == "none" for r in rows)

    async def _record(self, decision: Decision, *, message: str) -> None:
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                new_id = await conn.fetchval(
                    "INSERT INTO sali.proactive_decision "
                    "(kind, subject_ref, decision, reason_codes, message) "
                    "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                    decision.kind, decision.subject_ref,
                    "sent" if decision.send else "suppressed",
                    ",".join(decision.reason_codes)[:500],
                    (message or "")[:500])
            # Carry the id back so the sender can put it on the wire — this is what lets the phone
            # later say "he saw it and chose not to reply", which must never be scored as a snub.
            if new_id is not None:
                decision.id = str(new_id)

    async def _emit(self, decision: Decision) -> None:
        if self._publisher is None:
            return
        event_type = ("proactive.message.sent" if decision.send
                      else "proactive.message.suppressed")
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, subject_type="proactive", origin="background",
                data={"kind": decision.kind, "subject_ref": decision.subject_ref,
                      "reason_codes": decision.reason_codes})
