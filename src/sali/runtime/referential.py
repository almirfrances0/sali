"""Referential execution language (spec §10).

When Almir has just been shown a runnable command and then says "run it" / "do it" / "go ahead", he is
DELEGATING execution of that exact proposal — not asking for a new command. The loop must resolve the
reference against the captured pending action and run precisely that, never let the model reconstruct a
(possibly wrong) command from flattened context. This module is the deterministic half of that: a
closed-set classifier for delegation phrases, and a conservative extractor that recognises a runnable
command in Sali's own reply so it can be captured as the pending action. No model, no I/O — pure text.
"""

from __future__ import annotations

import re

# Phrases whose whole meaning is "execute the thing you just proposed". Matched exactly (after
# normalising whitespace/trailing punctuation) OR as a contained phrase, so "hey i want you to run it"
# still resolves. Kept deliberately narrow — bare "yes"/"ok" are NOT here (too ambiguous to auto-run).
_DELEGATION_EXACT = frozenset({
    "run it", "run that", "run this", "do it", "do that", "execute it", "execute that",
    "install it", "install that", "apply it", "apply that", "go ahead", "go for it", "proceed",
    "run", "execute", "send it", "just do it", "just run it", "do it now", "run it now",
    "yes do it", "yes run it", "do what you said", "do what u said", "make it happen",
    "go ahead and do it", "run the command", "execute the command", "run the command you said",
})
_DELEGATION_PHRASES = (
    "run it", "run that", "run this", "do it", "do that", "execute it", "execute that",
    "install it", "install that", "apply it", "apply that", "do what you said", "do what u said",
    "make it happen", "go for it", "just run", "run the command", "go ahead and",
)


_QUESTION_WORDS = frozenset({"how", "what", "why", "when", "where", "which", "who", "explain", "meaning"})


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().rstrip(".!?").split())


def is_delegation(text: str) -> bool:
    """True when the user is delegating execution of the pending proposal ("run it", "go ahead", …).
    A QUESTION about running something ("tell me how to run it", "how do i run it") is NOT delegation —
    detected by the OPENING word, so an imperative like "do what you said" still counts."""
    t = _norm(text)
    if not t:
        return False
    first = t.split()[0]
    if first in _QUESTION_WORDS or t.startswith(("tell me", "should i", "can you", "do you", "could you")):
        return False
    if t in _DELEGATION_EXACT:
        return True
    return any(p in t for p in _DELEGATION_PHRASES)


# ── Pure-social gate (tool availability, NOT delegation) ─────────────────────────────────────────
# A turn that provably needs no tools: a greeting, thanks, acknowledgement, farewell, or brief
# smalltalk with no request to act on the world. Used ONLY to decide whether to skip the ~10.6k-token
# native tools= payload. The classifier is deliberately CONSERVATIVE and closed-set: a false "social"
# (skipping tools when the turn actually needs them) leaves the model unable to act, so it fabricates
# an answer or prints tool-call syntax as text — catastrophic. A false "not social" (sending tools on a
# greeting) merely costs tokens. So anything NOT clearly inside the social vocabulary returns False and
# the tools are sent. No model, no I/O — pure text.
_SOCIAL_EXACT = frozenset({
    "hi", "hii", "hiii", "hello", "helo", "hey", "heya", "hiya", "yo", "sup", "hi there", "hello there",
    "hi sali", "hii sali", "hello sali", "hey sali", "yo sali", "heya sali", "hey there", "hi there sali",
    "good morning", "morning", "mornin", "gm", "good afternoon", "afternoon", "good evening", "evening",
    "good night", "goodnight", "gn", "night", "nite",
    "bye", "goodbye", "see ya", "see you", "cya", "later", "laters", "ttyl", "peace", "take care",
    "thanks", "thank you", "thx", "thnx", "ty", "tysm", "cheers", "much appreciated", "appreciate it",
    "appreciated", "thanks a lot", "thanks so much",
    "ok", "okay", "okey", "k", "kk", "alright", "aight", "cool", "nice", "great", "awesome", "sweet",
    "perfect", "lovely", "sounds good", "got it", "gotcha", "understood", "noted", "makes sense",
    "yep", "yup", "yeah", "yes", "yea", "no", "nope", "nah", "sure", "fine", "right",
    "mhm", "mm", "mmm", "hmm", "hm", "haha", "hahaha", "hehe", "lol", "lmao", "rofl", "nice one",
    "np", "no problem", "no worries", "yw", "you're welcome", "youre welcome", "welcome", "my pleasure",
    "how are you", "how r u", "how are u", "hows it going", "how's it going", "how you doing",
    "how are things", "whats up", "what's up", "wassup", "wsp",
    "im here", "i'm here", "im just here", "i'm just here", "just here", "just saying hi",
    "nothing", "nothing much", "just chilling", "im good", "i'm good", "all good", "im fine", "i'm fine",
})
# Filler words allowed alongside social words in a short multi-word greeting ("thanks man", "hi sali :)").
_SOCIAL_FILLER = frozenset({
    "sali", "man", "bro", "dude", "buddy", "friend", "mate", "there", "so", "well", "and", "too",
    "again", "then", "now", ":)", ":d", ":-)", "xd", "😁", "👍", "🙏", "😊", "❤️", "🙂", "🎉",
})
# Any of these in a short message means "there is something in the world to act on" — it vetoes the
# social classification even if the rest reads chatty ("ok run the scan", "hey what's the pc status").
_ACTION_HINT = re.compile(
    r"\b(run|do|does|did|execute|install|open|close|start|stop|restart|reboot|enable|disable|turn|"
    r"check|checks|search|find|look|fetch|get|show|tell|list|read|write|create|make|made|build|delete|"
    r"remove|move|copy|kill|fix|update|upgrade|set|change|scan|status|state|file|files|folder|dir|"
    r"command|cmd|bluetooth|wifi|wi-fi|network|internet|domain|site|website|url|owner|email|phone|"
    r"whois|dig|curl|wget|ssh|psql|task|scrape|download|upload|model|memory|about|who|what|when|where|"
    r"why|how many|which|report|analyse|analyze|schedule|remind|plan|help me)\b"
)


def is_pure_social(text: str) -> bool:
    """True ONLY for a turn that provably needs no tools — a greeting/thanks/ack/farewell/smalltalk with
    no request to act. Conservative by construction: anything outside the closed social vocabulary, or
    containing any actionable/world token, returns False so the native tools= payload is still sent.
    (Note: this is the inverse concern of is_delegation; the two are unrelated classifiers.)"""
    t = _norm(text)
    if not t:
        return False               # empty → don't strip tools; let the normal path decide
    # Drop a trailing address to the assistant ("thanks sali", "how are you sali") — it's not content
    # and would otherwise defeat the closed-set match on exactly the greetings Almir actually types.
    t = (t.removesuffix(" sali").strip() or t)
    if _ACTION_HINT.search(t):
        return False               # any actionable/world token → the turn may need tools
    if t in _SOCIAL_EXACT:
        return True
    words = t.split()
    if len(words) <= 4 and all(
        w.strip(",:;!?.") in _SOCIAL_EXACT or w.strip(",:;!?.") in _SOCIAL_FILLER for w in words
    ):
        return True
    return False


# Commands Sali can actually run — the first token that names one of these marks a runnable line. Broad
# and tool-agnostic (package/service/git/php/python/docker/ssh/…); NOT apt-specific.
_CMD_START = re.compile(
    r"^(?:sudo\s+)?(?:apt(?:-get)?|dpkg|pip3?|pipx|npm|npx|yarn|pnpm|composer|artisan|git|systemctl|"
    r"service|php|python3?|docker(?:-compose)?|curl|wget|make|cargo|go|node|deno|bash|sh|\./|chmod|"
    r"chown|mkdir|touch|ln|cp|mv|rsync|scp|ssh|psql|mysql|redis-cli|snap|flatpak|ip|ufw|nginx|apache2|"
    r"certbot|openssl|tar|unzip|kill|pkill|export)\b"
)


def _clean(line: str) -> str:
    return line.strip().lstrip("$>").strip().strip("`").strip()


def extract_proposed_command(text: str) -> dict[str, str] | None:
    """Recognise the primary runnable command Sali PROPOSED in its reply, so it becomes the pending
    action. Conservative: prefers a fenced/backticked command, then a bare command line; returns None
    when nothing clearly runnable is present (never guess). First match wins — the primary proposal."""
    # 1) fenced code blocks
    for block in re.findall(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", text, re.S):
        for raw in block.splitlines():
            line = _clean(raw)
            if line and _CMD_START.match(line):
                return {"command": line, "description": _describe(line)}
    # 2) inline `backtick` commands
    for span in re.findall(r"`([^`\n]+)`", text):
        line = _clean(span)
        if _CMD_START.match(line):
            return {"command": line, "description": _describe(line)}
    # 3) a bare command line (kept short so we don't grab prose that merely starts with a verb)
    for raw in text.splitlines():
        line = _clean(raw)
        if _CMD_START.match(line) and 1 <= len(line.split()) <= 14:
            return {"command": line, "description": _describe(line)}
    return None


def _describe(command: str) -> str:
    parts = command.split()
    if "install" in parts:
        i = parts.index("install")
        pkgs = " ".join(p for p in parts[i + 1:] if not p.startswith("-"))
        if pkgs:
            return f"Install {pkgs}"
    return "Run: " + " ".join(parts[:6]) + (" …" if len(parts) > 6 else "")
