"""What Sali has LEARNED about his own machine — the durable answer to "can I actually do that here?"

The example this exists for: today `ping` answers "command not found"; tomorrow Sali must already know
that, before he reaches for it again.

Why this is separate from ``tool_experience`` (which mines the same executions): that miner is
STATISTICAL and evidence-gated — it will not characterise a binary until it has ``threshold`` runs,
because a success *rate* from one sample is meaningless. A machine constraint is not statistical. One
unambiguous "command not found" is a complete, deterministic observation about this host, and waiting
for three of them means Sali fails the same way three times before he is allowed to notice. So the two
live side by side: reliability stays gated, constraints land on the first occurrence.

THREE GUARDS make that safe, because a mechanism that turns failures into "I can't" is exactly the kind
that rots into learned helplessness:

1. ONLY DETERMINISTIC CLASSES. A constraint is written only when the error says the BINARY ITSELF is
   unavailable or forbidden — absent from the machine, or refused for want of privilege. A timeout, a
   bad flag, a missing *argument* file, a network blip: none of those are facts about the machine, and
   none are recorded. TRANSIENT failures in particular must never become "I can't do this."
2. THE BINARY MUST BE NAMED. The error has to actually mention the program. "No such file or directory"
   about some path the command was *given* says nothing about whether the command exists.
3. SUCCESS RETIRES IT. The moment that binary runs successfully, the constraint is closed. This is the
   self-heal: install the package, and Sali stops believing it is missing without anyone telling him.
   Kept as a bitemporal close (``valid_until``), never a DELETE, so the history stays auditable.

Stored as MemoryLayer.SYSTEM_ENV / MemorySource.TOOL_RESULT under ``env:tool:<binary>``. SYSTEM_ENV is
deliberate: it is the one layer the grounding faculty is allowed to RE-OBSERVE, so a constraint can be
re-checked against the machine rather than believed forever.
"""

from __future__ import annotations

import re
from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger

log = get_logger("sali.learning.environment")

# The binary is ABSENT. Each is a shell/loader statement about the PROGRAM. They are matched
# ADJACENTLY (see _absent_of) — "no such file or directory" is only about the binary when it comes
# directly after it; `cat: notes.txt: No such file or directory` is about the argument, and reading
# that as "cat is not installed" is exactly the over-learning this module must not do.
_ABSENT = (
    "command not found",
    "not found",                    # dash/sh phrasing: "sh: 1: ping: not found"
    "not installed",
    "no such file or directory",
    "executable file not found",
    "unknown command",
)
# zsh puts it the other way round: "zsh: command not found: ping".
_ABSENT_PREFIX = ("command not found:", "unknown command:")

# The binary EXISTS but this user may not use it this way. Privilege errors legitimately carry
# context between the program and the complaint ("tcpdump: eth0: You don't have permission"), so
# adjacency is too strict here; _forbidden_of uses a path test instead.
_FORBIDDEN = (
    "permission denied",
    "operation not permitted",
    "not permitted",
    "must be root",
    "must be run as root",
    "are you root",
    "requires root",
    "need to be root",
    "have permission",
    "eperm",
    "eacces",
)
# Never a statement about the machine — the anti-helplessness list.
_NOT_A_CONSTRAINT = (
    "timed out", "timeout", "connection refused", "connection reset", "network is unreachable",
    "no route to host", "temporarily unavailable", "try again", "broken pipe",
    "invalid option", "unrecognized option", "usage:", "invalid argument",
)

MISSING = "missing"
NEEDS_PRIVILEGE = "needs_privilege"


def _first_line(text: str | None) -> str:
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


def _segments_after(binary: str, err: str) -> list[str]:
    """Everything following each mention of the binary, lowercased. Empty when it is never named —
    which is itself a rejection: an error that does not mention the program says nothing about it."""
    b = binary.lower()
    err = err.lower()
    out, start = [], 0
    while (i := err.find(b, start)) != -1:
        out.append(err[i + len(b):])
        start = i + len(b)
    return out


def _absent_of(binary: str, err: str) -> bool:
    """True only when an absent-marker sits IMMEDIATELY after the binary (bar a ':' and spaces), or
    when the shell used the prefix form. That adjacency is the whole guard."""
    low = err.lower()
    b = binary.lower()
    for pre in _ABSENT_PREFIX:
        if pre in low and low.split(pre, 1)[1].strip().startswith(b):
            return True
    for tail in _segments_after(binary, err):
        rest = tail.lstrip()
        while rest[:1] in (":", "-"):
            rest = rest[1:].lstrip()
        if any(rest.startswith(m) for m in _ABSENT):
            return True
    return False


def _forbidden_of(binary: str, err: str) -> bool:
    """True when the privilege complaint is about the PROGRAM, not about a path it was handed.

    `cat: /etc/shadow: Permission denied` is a fact about that file — cat is fine, and recording
    "cat needs privileges" would be false. `tcpdump: eth0: You don't have permission to capture` is
    a fact about running tcpdump as this user. The discriminator that separates them cleanly is a
    filesystem path between the program and the complaint."""
    for tail in _segments_after(binary, err):
        for marker in _FORBIDDEN:
            i = tail.find(marker)
            if i == -1:
                continue
            between = tail[:i]
            if "/" in between:
                continue          # the complaint belongs to a path, not to the program
            return True
    return False


def _names_binary(binary: str, err: str) -> bool:
    """The error must actually be about this program."""
    return bool(binary) and binary.lower() in err.lower()


# What a program name can actually look like. `binary_of()` returns the first token of whatever was
# run, and when Sali runs something malformed — pasting a shell error back as a command, say — that
# token can be a fragment like `bash:`. The shell then answers "bash: line 1: bash:: command not
# found", which is a perfectly true statement about a thing that was never a program. Live example:
# this recorded `env:tool:bash:` as a machine constraint, and Sali then talked about "retiring the
# stale belief that bash is missing" in Almir's chat.
_PLAUSIBLE_BINARY = re.compile(r"^[A-Za-z0-9._/+-]+$")


def _is_program_name(binary: str) -> bool:
    """A constraint is only worth recording about something that could be a program."""
    if not binary or len(binary) > 64:
        return False
    if not _PLAUSIBLE_BINARY.match(binary):
        return False              # colons, quotes, spaces → an error fragment, not a command
    if binary.startswith("-"):
        return False              # a flag that got parsed as the command, e.g. `--version`
    return not binary.endswith((".", "-", "/"))


def classify_constraint(binary: str, error_text: str | None) -> str | None:
    """The machine-fact in this failure, or None if the failure says nothing durable about the machine.

    Pure and deterministic — no model, no I/O. None is the common, correct answer."""
    err = (error_text or "").strip()
    if not err or not _is_program_name(binary):
        return None
    low = err.lower()
    if any(s in low for s in _NOT_A_CONSTRAINT):
        return None                                  # Guard 1: transient/usage is never a constraint
    if not _names_binary(binary, err):
        return None                                  # Guard 2a: not about this program at all
    if _forbidden_of(binary, err):
        return NEEDS_PRIVILEGE
    if _absent_of(binary, err):                      # Guard 2b: adjacency
        return MISSING
    return None


def _content(binary: str, constraint: str, evidence: str) -> str:
    if constraint == MISSING:
        return (f"`{binary}` is not available on this machine — running it fails with "
                f'"{evidence}". Use an installed alternative, or install it first.')
    return (f"`{binary}` exists on this machine but this user cannot run it as used — it fails with "
            f'"{evidence}". It needs elevated privileges.')


async def note_tool_outcome(
    conn: Any, *, binary: str, success: bool, error_text: str | None = None,
) -> str | None:
    """Fold one command outcome into what Sali knows about this machine.

    On a qualifying FAILURE: record (or reinforce) the constraint. On SUCCESS: retire any constraint
    held against that binary — guard 3, the self-heal. Returns the action taken, for logging/tests.
    Caller owns the transaction; fail-open is the caller's job."""
    if not binary:
        return None
    claim_key = f"env:tool:{binary}"

    if success:
        closed = await conn.fetchval(
            "UPDATE memory SET valid_until = now(), updated_at = now() "
            "WHERE claim_key = $1 AND valid_until IS NULL RETURNING id", claim_key)
        if closed is not None:
            log.info("env_constraint_retired", binary=binary)
            return "retired"
        return None

    constraint = classify_constraint(binary, error_text)
    if constraint is None:
        return None

    evidence = _first_line(error_text)
    content = _content(binary, constraint, evidence)
    structured = {
        "binary": binary, "constraint": constraint, "evidence": evidence,
        "certainty": "observed",
    }
    existing = await conn.fetchrow(
        "SELECT id, evidence_count, structured FROM memory "
        "WHERE claim_key = $1 AND valid_until IS NULL", claim_key)
    if existing is not None:
        prior = existing["structured"] if isinstance(existing["structured"], dict) else {}
        # Seeing it again is corroboration, not a new claim. Confidence climbs but never reaches
        # certainty: this is still an observation about a machine that can change under us.
        count = int(existing["evidence_count"] or 1) + 1
        conf = min(0.92, 0.75 + 0.04 * count)
        if prior.get("constraint") == constraint:
            await conn.execute(
                "UPDATE memory SET evidence_count = $2, confidence = $3, last_seen = now(), "
                "  last_verified = now(), updated_at = now() WHERE id = $1",
                existing["id"], count, conf)
            return "reinforced"
        # The constraint CHANGED (missing -> needs_privilege, say: the package got installed but the
        # command needs root). Close the old statement rather than silently overwriting it.
        await conn.execute(
            "UPDATE memory SET valid_until = now(), updated_at = now() WHERE id = $1",
            existing["id"])

    await memory_writer.remember(
        conn, layer=MemoryLayer.SYSTEM_ENV, content=content, source=MemorySource.TOOL_RESULT,
        functional=True, claim_key=claim_key, importance=0.6, obs_conf=0.75, structured=structured)
    log.info("env_constraint_learned", binary=binary, constraint=constraint)
    return constraint


async def learned_constraints(conn: Any, *, limit: int = 6) -> list[dict[str, Any]]:
    """The currently-believed machine constraints, most recently confirmed first — the read side."""
    rows = await conn.fetch(
        "SELECT content, structured, confidence, last_seen FROM memory "
        "WHERE claim_key LIKE 'env:tool:%' AND valid_until IS NULL AND superseded_by IS NULL "
        "ORDER BY last_seen DESC NULLS LAST LIMIT $1", limit)
    out: list[dict[str, Any]] = []
    for r in rows:
        s = r["structured"] if isinstance(r["structured"], dict) else {}
        if not s.get("binary"):
            continue
        out.append({"binary": s["binary"], "constraint": s.get("constraint"),
                    "evidence": s.get("evidence"), "confidence": float(r["confidence"] or 0)})
    return out


def render_constraints(rows: list[dict[str, Any]]) -> str:
    """One compact prompt block. Empty string when Sali has learned nothing yet — a heading with no
    content under it is worse than silence, and this ships on a machine with zero rows."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        why = "not installed here" if r["constraint"] == MISSING else "needs elevated privileges here"
        lines.append(f"- {r['binary']}: {why} (learned from a real attempt)")
    return ("What you have learned you CANNOT do on this machine (check before reaching for these):\n"
            + "\n".join(lines))
