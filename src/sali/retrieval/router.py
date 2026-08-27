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
# Questions about Sali's OWN tools/capabilities — "which tools do I have?", "is there a tool for X?".
# These pull from the tool inventory + capability graph, not just memory.
_TOOL = (
    "what tool", "which tool", "what tools", "which tools", "tools do i", "tools are",
    "tool for", "tools for", "do i have", "have a tool", "is there a tool", "any tool",
    "installed", "what's installed", "whats installed", "list tools", "what can i use",
    "tools can i", "tool to ", "tools to ", "tools i have", "tools available",
)
# A doing/fixing request — pull how Sali handled this BEFORE (procedures) and what went wrong last
# time (past incidents), so it never starts from zero (§4/§6).
_TASK = (
    "how do i", "how do you", "how to", "how can i", "how should i", "how would you",
    "deploy", "set up", "set-up", "setup", "configure", "fix", "debug", "troubleshoot",
    "broken", "not working", "doesn't work", "does not work", "won't", "wont ", "isn't working",
    "failing", "keeps failing", "getting an error", "throwing", "get it working", "make it work",
    "again",  # "docker is broken again" — a recurrence is exactly when past experience matters
)


@dataclass(slots=True)
class RetrievalPlan:
    intent: str
    use_vector: bool = True
    use_keyword: bool = True
    use_graph: bool = False
    use_recent: bool = False
    needs_live: bool = False
    use_tools: bool = False
    use_experience: bool = False  # a doing/fixing turn — pull past procedures + incidents


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
    tool = _has(q, _TOOL)
    task = _has(q, _TASK)
    intent = (
        "live" if live
        else "task" if task
        else "tool" if tool
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
        use_tools=tool,
        use_experience=task,
    )
