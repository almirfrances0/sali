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
