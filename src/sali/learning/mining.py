"""Deterministic mining of the durable action record (spec §17/§19).

Finds recurring command *sequences* across runs — the raw material for a learned procedure. No
model is involved here (that's the INTERPRET step, done later): detecting that the same steps
ran twice is a parse, not a judgement call. Commands are normalized to their action skeleton
(program + a couple of subcommands, dropping variable args/paths) so "docker compose up -d /x"
and "docker compose up -d" collapse to the same step.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

_MAX_STEP_TOKENS = 3
_BREAK_PREFIX = ("-", "/", ".", "~", "$", "'", '"')


def normalize_command(command: str) -> str:
    """Reduce a command to its stable action skeleton: the program plus up to two subcommands,
    stopping at the first flag, path, value, or number (which vary run to run)."""
    toks: list[str] = []
    for tok in command.strip().split():
        if tok.startswith(_BREAK_PREFIX) or "=" in tok or any(c.isdigit() for c in tok):
            break
        toks.append(tok.lower())
        if len(toks) >= _MAX_STEP_TOKENS:
            break
    return " ".join(toks)


def sequences_by_run(rows: list[dict[str, Any]]) -> dict[tuple[str, ...], set[UUID]]:
    """Group per-run command lists (rows ordered by run then time) into normalized sequences,
    and return each distinct sequence → the set of runs it appeared in (its evidence)."""
    by_run: dict[UUID, list[str]] = {}
    for row in rows:
        norm = normalize_command(str(row["command"]))
        if not norm:
            continue
        steps = by_run.setdefault(row["run_id"], [])
        if not steps or steps[-1] != norm:  # collapse consecutive repeats
            steps.append(norm)
    out: dict[tuple[str, ...], set[UUID]] = {}
    for run_id, steps in by_run.items():
        if len(steps) >= 2:  # a single command isn't a procedure
            out.setdefault(tuple(steps), set()).add(run_id)
    return out


def signature(steps: tuple[str, ...]) -> str:
    """A stable id for a sequence, used as the procedure's claim_key so re-learning updates it."""
    return hashlib.sha256("|".join(steps).encode("utf-8")).hexdigest()[:16]
