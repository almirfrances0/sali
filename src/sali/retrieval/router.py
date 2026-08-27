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
# A query whose SUBJECT is Sali's own host/self/environment — "analyse the system", "check your PC",
# "what OS am I on", "my GPU/RAM/disk/services/processes". These must be answered from CURRENT world/self
# state, not stale semantic memory (§4/§11), so they force needs_live and demote the memory pool.
_SYSTEM = (
    "the system", "this system", "this machine", "this computer", "this host", "this pc", "this box",
    "the machine", "my machine", "my computer", "my system", "my host", "my pc", "my box", "my setup",
    "my environment", "my desktop", "my gpu", "my cpu", "my ram", "my memory", "my disk", "my storage",
    "my network", "my services", "my processes", "my ports", "my hardware", "my kernel", "my os",
    "your machine", "your computer", "your system", "your host", "your pc", "your gpu", "your box",
    "your desktop", "your environment", "your kernel", "your os", "check your pc", "check the system",
    "analyse the system", "analyze the system", "analyse my", "analyze my", "audit this machine",
    "audit the system", "diagnose the environment", "diagnose the system", "scan the system",
    "what host", "hostname", "what os", "which os", "what machine", "which machine", "what computer",
    "am i on", "where do i live", "where am i running", "what's running", "whats running",
    "what is running", "running services", "running processes", "system state", "environment state",
    "what gpu", "which gpu", "gpu do i", "what cpu", "which cpu", "cpu do i", "what hardware",
    "hardware do i", "which hardware", "vram", "uptime", "how much ram", "how much memory",
    "how much disk", "how much vram", "how much storage", "how much space", "what's my ip", "whats my ip",
    "what services", "which services", "services running", "services are", "what processes",
    "which processes", "processes are", "what ports", "which ports", "ports are", "ports open",
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
    system_query: bool = False  # subject is Sali's own host/self/environment — prefer live over memory


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
    system = _has(q, _SYSTEM)  # subject is Sali's own machine/self/environment
    intent = (
        "live" if live
        else "task" if task
        else "tool" if tool
        else "system" if system
        else "relational" if relational
        else "temporal" if temporal
        else "recent" if recent
        else "lookup"
    )
    return RetrievalPlan(
        intent=intent,
        use_graph=relational,
        use_recent=recent or temporal,
        needs_live=live or system,  # a system/environment question always checks current reality (§3/§4)
        use_tools=tool,
        use_experience=task,
        system_query=system,
    )
