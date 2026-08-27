"""Deterministic query router.

Different questions need different sources of truth (spec §12): "what's my current VRAM?"
needs a live tool, "which projects are on the VPS?" needs graph traversal, "what did I say
about models?" needs semantic memory. This classifies the query with cheap deterministic
cues — no LLM — and the agent loop dispatches accordingly. Semantic + keyword memory search
is always on; the flags add graph, recency, and the live-inspection signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Self-contained turns that need NO memory (§35: don't search 500 memories for "2+2"). Conservative —
# only obvious greetings/acks and pure arithmetic; anything else still retrieves.
_TRIVIAL_EXACT = frozenset({
    "hi", "hello", "hey", "yo", "sup", "hiya", "hey there", "hi there", "hello there",
    "thanks", "thank you", "thankyou", "ty", "cheers", "much appreciated",
    "ok", "okay", "k", "kk", "yes", "yeah", "yep", "no", "nope", "sure", "got it",
    "cool", "nice", "great", "awesome", "perfect", "lol", "haha", "nvm", "never mind",
    "bye", "goodbye", "good morning", "good night", "gm", "gn", "morning",
})
_ARITH = re.compile(r"^[\s\d+\-*/().,^%=x×÷]+$")
_ARITH_PREFIX = re.compile(r"^(what\s+is|what's|whats|calculate|compute|solve|how much is)\s+")

_LIVE = (
    "right now", "currently", "current ", "at the moment", "how much", "how many",
    "is running", "running?", "live ", "today", "this moment",
)
_TEMPORAL = (
    "yesterday", "last week", "last month", "days ago", "weeks ago", "months ago",
    "earlier", "previously", "used to", "history", "over time", "when did", "back then",
)
_RELATIONAL = (
    "connected", "related", "depends on", "depend on", "runs on", "part of", "linked to",
    "associated", "which project", "what uses", "who owns", "hosted", "deployed", "where is",
    "located", "lives on", "belongs to", "runs the",
)
_RECENT = ("recent", "lately", "just now", "what happened", "what changed", "changes since")


@dataclass(slots=True)
class RetrievalPlan:
    intent: str
    use_vector: bool = True
    use_keyword: bool = True
    use_graph: bool = False
    use_recent: bool = False
    needs_live: bool = False


def _has(query: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in query for phrase in phrases)


def _is_trivial(q: str) -> bool:
    s = q.strip(" ?!.\t\n")
    if not s:
        return True
    if s in _TRIVIAL_EXACT:
        return True
    core = _ARITH_PREFIX.sub("", s)  # strip a "what is …" wrapper around a sum
    return bool(_ARITH.match(core) and any(ch.isdigit() for ch in core))


def classify(query: str) -> RetrievalPlan:
    q = query.lower()
    if _is_trivial(q):  # a greeting, an ack, or plain arithmetic — self-contained, needs no memory
        return RetrievalPlan(intent="trivial", use_vector=False, use_keyword=False)
    live = _has(q, _LIVE)
    temporal = _has(q, _TEMPORAL)
    relational = _has(q, _RELATIONAL)
    recent = _has(q, _RECENT)
    intent = (
        "live" if live
        else "relational" if relational
        else "temporal" if temporal
        else "recent" if recent
        else "lookup"
    )
    return RetrievalPlan(
        intent=intent,
        use_graph=relational,
        use_recent=recent or temporal,
        needs_live=live,
    )
