"""Sali's own words for the things he says unprompted.

Every message Sali sent on his own initiative used to be a Python f-string — "Heads up — you said
you'd {desc}", "Finished: {objective}." A person doesn't have four sentences he recites; he looks at
what happened and says something about it. This is the half that lets him do that.

WHAT DID NOT CHANGE, DELIBERATELY: whether and when he speaks. That stays fully deterministic in
CommunicationDecisionEngine's five gates, because that is what keeps him from being annoying — and
being annoying is the failure mode that reads as "robot". This module only decides the WORDING of a
message that the gates have already agreed should exist.

Three properties make that safe to do from a background faculty:

1. IT NEVER COMPETES WITH ALMIR. The one 35B model on this box sits behind a machine-wide lock, and a
   raw generation is invisible to the coordinator — `_yield_background` cannot cancel it. So the
   composer refuses to start whenever foreground work is demanded or in flight, and re-checks after,
   with a hard timeout. Sali finding nicer words for himself must never be the reason Almir waits.

2. IT CANNOT INVENT. A template can't fabricate a deadline; a sentence can. The composed text is
   checked back against the fact bundle it was given: every number, path, URL and quoted phrase in
   the output has to appear in the facts. Anything it made up, and the composition is discarded.

3. IT CANNOT FAIL LOUDLY. Every guard, every exception, every rejection returns the deterministic
   template. The old wording is the FLOOR, so at worst Sali says exactly what he says today.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

from sali.obs.log import get_logger
from sali.provider.base import ChatMessage
from sali.provider.presets import CREATIVE, DETERMINISTIC

log = get_logger("sali.cognitive.voice")

# There is exactly one model on this machine and one process that owns it, so the composer is bound
# once at boot rather than threaded through five unrelated call sites (a task store has no business
# holding a provider handle). Unbound — tests, CLI probes, anything that isn't the live daemon — every
# call returns the caller's fallback, which is the wording that shipped before this module existed.
_provider: Any = None
_coordinator: Any = None
_pool: Any = None


def bind(*, provider: Any, coordinator: Any = None, pool: Any = None) -> None:
    """Give Sali his voice. Called once, from the daemon, after the provider exists.

    `pool` is what lets him see the conversation he is already part of. Without it he can still
    speak, but only about the facts in front of him — which is the difference between someone
    picking a thread back up and a notification with good grammar.
    """
    global _provider, _coordinator, _pool
    _provider = provider
    _coordinator = coordinator
    _pool = pool


def is_bound() -> bool:
    return _provider is not None


# Sampling: the same warm profile his spoken replies use. CREATIVE is passed EXPLICITLY because the
# provider's default preset is DETERMINISTIC, which carries seed=42 — a fixed seed would give him the
# same sentence for the same situation every time, which is just a template with extra steps.
_VOICE_OPTS: dict[str, Any] = {
    "temperature": 0.8, "top_k": 40, "top_p": 0.95,
    "presence_penalty": 0.3, "repeat_penalty": 1.1, "repeat_last_n": 512,
    # A hard ceiling in TOKENS as well as characters. The push transport trims a banner at 240 chars,
    # so a long message doesn't arrive as prose — it arrives as garbage with the end cut off.
    "num_predict": 110,
}

_BRIEF = (
    "\n\nRIGHT NOW you are STARTING this conversation. Almir has not said anything — you noticed "
    "something yourself and decided it was worth telling him, so you are the one walking up. "
    "\n\nOpen like a person does. You cannot approach someone and begin mid-thought with a bare "
    "fact — that is the thing that sounds like a machine. Land first, then say it: a couple of words "
    "that acknowledge you are the one arriving, and if the two of you were in the middle of "
    "something earlier, pick that thread up instead of ignoring it. Vary how you open; you are not "
    "reading from a script, and the same phrase every time is worse than none. If he is in the "
    "middle of something, be brief about it. "
    "\n\nThen say the thing, in your own voice — the same way you talk to him when you answer. Two "
    "or three sentences, first person, plain text, no sign-off. "
    "\n\nEVERY FACTUAL CLAIM MUST COME FROM 'WHAT YOU KNOW' BELOW — that is the only source of "
    "facts you have. Earlier conversation is there so you sound like yourself and can pick a thread "
    "back up; it is NOT a source of facts, and neither is what seems likely. If you only know one "
    "thing, say the one thing. Never say you did something that isn't listed there — you did not "
    "check, read, test or fix anything unless it says you did. Names, numbers, paths and times must "
    "match exactly: do not add, round, or guess. Don't repeat yourself. Don't ask him for anything "
    "unless the situation genuinely needs a decision from him."
)

_CHECK_SYS = (
    "You check one message against the facts its author was given. Answer with exactly one line.\n"
    "If EVERY factual statement in the message is supported by the facts, answer: OK\n"
    "If the message states anything the facts do not support — a different cause, an extra detail, a "
    "claim of having done something not listed — answer: BAD followed by the unsupported words.\n"
    "Opinions, greetings, and how it is worded do not matter. Only whether the claims are supported."
)

# Room for an opening AND the point. The push banner trims at 240, so a longer message is clipped
# in the lock-screen preview — that is the right trade: the chat is where he reads it, and a message
# that has to skip the greeting to fit a notification is the robot voice all over again.
_MAX_CHARS = 480
_TIMEOUT_S = 25.0

# Shapes that mean the model slipped out of conversation and into output-formatting mode.
_BAD_SHAPE = re.compile(r"```|<\|?(tool|function|im_start)|^\s*[\[{]", re.IGNORECASE)
_NUM = re.compile(r"\d+(?:[.,:]\d+)*")
_PATHISH = re.compile(r"(?:https?://\S+|/[\w.\-/]{2,}|\b[\w\-]+\.[a-z]{2,}\b)", re.IGNORECASE)
_QUOTED = re.compile(r"[\"'`]([^\"'`\n]{3,})[\"'`]")


def _facts_text(facts: dict[str, Any]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, "", []))


def _norm(s: str) -> str:
    return re.sub(r"[\s,]+", "", s).lower()


def _invents_anything(text: str, facts_blob: str) -> str | None:
    """Return the first token the composition asserts that its facts never mentioned.

    This is the anti-fabrication gate, and it is deliberately DUMB — a substring comparison, not a
    judgement. A smart check would need the model that just wrote the sentence to grade itself.
    """
    hay = _norm(facts_blob)
    # Numbers are matched as a SET, by exact token — never as substrings. A substring test passes
    # almost anything short: "was due Sep 9" checks out against facts containing "Sep 5 at 17:00"
    # purely because a 9 appears somewhere in the blob. That is precisely the fabrication this exists
    # to catch — a wrong date about Almir's own commitment, stated in his own assistant's voice — so
    # every number in the sentence has to be a number he was actually given.
    known = {_norm(t) for t in _NUM.findall(facts_blob)}
    for m in _NUM.finditer(text):
        tok = m.group(0)
        if _norm(tok) not in known:
            return tok
    for m in _PATHISH.finditer(text):
        tok = m.group(0).rstrip(".,;:!?)")
        if _norm(tok) not in hay:
            return tok
    for m in _QUOTED.finditer(text):
        tok = m.group(1)
        if _norm(tok) not in hay:
            return tok
    return None


async def _recent_exchange(limit: int = 8) -> str:
    """The last few things actually said between them, oldest first.

    This is the single biggest difference between a message that sounds like Sali and one that
    sounds like a notification: he replies with the thread in front of him, so he writes with it in
    front of him here too. Read-only, and nothing is written — an unprompted message must never
    manufacture a turn.
    """
    if _pool is None:
        return ""
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM sali.message "
                "WHERE created_at > now() - interval '12 hours' "
                "ORDER BY created_at DESC LIMIT $1", limit)
    except Exception:  # noqa: BLE001 - no history is survivable; a broken read is not worth a failure
        return ""
    if not rows:
        return ""
    out = []
    for r in reversed(rows):
        who = "Almir" if str(r["role"]) == "user" else "You"
        body = " ".join(str(r["content"] or "").split())[:400]
        if body:
            out.append(f"{who}: {body}")
    return "\n".join(out)


def _busy() -> bool:
    """True when ALMIR is waiting on a turn. His turn owns the model; this can always wait.

    Deliberately NOT `is_thinking`. That property is the single cognition slot being held by
    foreground OR background work, and background cognition runs constantly on an idle machine — so
    gating on it meant the composer declined almost every time and Sali fell back to the fixed
    wording this module exists to replace. Measured live: it declined with "almir is being served"
    while he had been idle for 34 minutes.

    Background work holding the slot is not somebody waiting. The 25s timeout bounds that case on
    its own: if the slot never frees, the composition simply gives up and the template ships.
    """
    c = _coordinator
    if c is None:
        return False
    with contextlib.suppress(Exception):
        return bool(getattr(c, "foreground_demanded", False))
    return False


async def _states_more_than_it_knows(text: str, facts_blob: str) -> str | None:
    """The unsupported claim, or None. Errs toward SHIPPING — a checker that cannot run must not
    silence him, and every other guard still applies."""
    if _provider is None:
        return None
    try:
        res = await asyncio.wait_for(
            _provider.chat(
                [ChatMessage(role="system", content=_CHECK_SYS),
                 ChatMessage(role="user",
                             content=f"FACTS:\n{facts_blob}\n\nMESSAGE:\n{text}")],
                options={"num_predict": 60}, preset=DETERMINISTIC),
            timeout=20.0)
    except Exception:  # noqa: BLE001
        return None
    verdict = " ".join((getattr(res, "content", "") or "").split()).strip()
    if verdict[:3].upper() == "BAD":
        return verdict[3:].strip(" :-") or "unsupported claim"
    return None


async def compose(*, situation: str, facts: dict[str, Any], fallback: str,
                  identity: str | None = None) -> str:
    """Sali's own sentence about `situation`, or `fallback` if anything at all is off.

    `situation` tells him what kind of moment this is; `facts` is everything true about it. The
    fallback is the deterministic template, so this can only ever improve on today's wording.
    """
    if _provider is None:
        log.info("voice_fallback", why="not bound", situation=situation[:50])
        return fallback
    if _busy():
        log.info("voice_fallback", why="almir is waiting on a turn", situation=situation[:50])
        return fallback
    from sali.context.engine import IDENTITY

    blob = _facts_text(facts)
    exchange = await _recent_exchange()
    where = None
    try:
        from sali.perception import presence

        where = presence.describe()
    except Exception:  # noqa: BLE001 - no display, no problem
        where = None

    parts = [f"WHY YOU ARE SPEAKING: {situation}"]
    if exchange:
        # Framed as what was said, not as a turn addressed to him — nothing here is Almir speaking
        # now, and Sali must not answer it as though it were.
        parts.append("THE LAST THINGS BETWEEN YOU (context only — he is not saying these now):\n"
                     + exchange)
    if where:
        parts.append(f"WHERE HE IS: {where}")
    parts.append(f"WHAT YOU KNOW:\n{blob}")
    parts.append("Write the message to him now.")

    try:
        res = await asyncio.wait_for(
            _provider.chat(
                [ChatMessage(role="system", content=(identity or IDENTITY) + _BRIEF),
                 ChatMessage(role="user", content="\n\n".join(parts))],
                options=_VOICE_OPTS, preset=CREATIVE),
            timeout=_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - model down, timeout, lease: say it the old way
        log.info("voice_fallback", why=f"{type(exc).__name__}: {exc}"[:120],
                 situation=situation[:50])
        return fallback

    text = " ".join((getattr(res, "content", "") or "").split()).strip().strip('"')
    if not text:
        log.info("voice_fallback", why="empty", situation=situation[:50])
        return fallback
    if len(text) > _MAX_CHARS:
        log.info("voice_fallback", why=f"too long ({len(text)} > {_MAX_CHARS})",
                 situation=situation[:50])
        return fallback
    if _BAD_SHAPE.search(text):
        log.info("voice_fallback", why="markdown/tool syntax", situation=situation[:50])
        return fallback
    # The conversation counts as things he KNOWS — otherwise picking up a thread ("that tunnel
    # thing from earlier") would be rejected as fabrication and he would be forced back into
    # context-free sentences, which is the robot voice this exists to fix.
    invented = _invents_anything(text, "\n".join([blob, situation, exchange or "", where or ""]))
    if invented is not None:
        # He wrote something the facts don't support. Never ship it, and keep the receipt — a rising
        # count here means the fact bundle is too thin for the sentence being asked of it.
        log.info("voice_fallback", why=f"invented {invented[:40]!r}", situation=situation[:50])
        return fallback
    # Last gate: the same grounding `send_agent_message` runs, applied BEFORE the decision is
    # committed. If grounding would strike any of it, the template goes instead of a hollowed-out
    # sentence with a canned apology stapled on.
    with contextlib.suppress(Exception):
        from sali.verify.response_claims import validate_proactive
        rv = await validate_proactive(text, cap_of={})
        if rv.changed:
            log.info("voice_fallback", why="grounding would strike it", situation=situation[:50])
            return fallback
    # Read it back against what he was told. The token check above catches invented NUMBERS; this
    # catches an invented explanation, which is the failure that actually happened: asked to share
    # one finding, he wrote a fluent and entirely different one, plus "I checked the docs again too".
    unsupported = await _states_more_than_it_knows(text, blob)
    if unsupported is not None:
        log.info("voice_fallback", why=f"unsupported claim: {unsupported[:70]}",
                 situation=situation[:50])
        return fallback
    if _busy():
        # Almir started typing while the model was writing. His turn is what matters; the message
        # still goes, just in the cheap words — nothing is dropped.
        log.info("voice_fallback", why="almir started while writing", situation=situation[:50])
        return fallback
    log.info("voice_composed", chars=len(text), situation=situation[:50])
    return text
