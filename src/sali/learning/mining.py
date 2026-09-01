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


def normalize_tool_step(tool_name: str, tool_args: dict[str, Any] | None) -> str:
    """Extract a stable step representation from any tool execution.

    For execute_command, normalizes the shell command. For other tools, uses
    tool_name plus a compact summary of key arguments (path, query, url, etc.)
    so repeated tool patterns can be mined into procedures.
    """
    if tool_name == "execute_command":
        command = (tool_args or {}).get("command", "")
        if isinstance(command, str) and command.strip():
            return normalize_command(command)
        return ""  # empty/null command — skip this execution
    # For non-command tools: tool_name + stable key args
    args = tool_args or {}
    key_parts: list[str] = [tool_name]
    # Extract the most meaningful arg for each tool type
    for key in ("path", "query", "url", "entity", "relation", "name", "to", "subject"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            # Take just the basename for paths, first 3 words for text
            if key == "path":
                from pathlib import Path
                key_parts.append(Path(val).name)
            else:
                key_parts.extend(val.lower().split()[:2])
            break  # one key arg is enough for the step signature
    return " ".join(key_parts[:_MAX_STEP_TOKENS + 1])


def sequences_by_run(rows: list[dict[str, Any]]) -> dict[tuple[str, ...], set[UUID]]:
    """Group per-run tool execution lists (rows ordered by run then time) into normalized sequences,
    and return each distinct sequence → the set of runs it appeared in (its evidence).

    Each row must have 'run_id' and either 'command' (for execute_command) or 'tool_name' +
    'tool_args' (for any tool).
    """
    by_run: dict[UUID, list[str]] = {}
    for row in rows:
        # Support both old format (command only) and new format (tool_name + tool_args)
        if "tool_name" in row:
            norm = normalize_tool_step(row["tool_name"], row.get("tool_args"))
        else:
            norm = normalize_command(str(row.get("command", "")))
        if not norm:
            continue
        steps = by_run.setdefault(row["run_id"], [])
        if not steps or steps[-1] != norm:  # collapse consecutive repeats
            steps.append(norm)
    out: dict[tuple[str, ...], set[UUID]] = {}
    for run_id, steps in by_run.items():
        if len(steps) >= 2:  # a single step isn't a procedure
            out.setdefault(tuple(steps), set()).add(run_id)
    return out


def signature(steps: tuple[str, ...]) -> str:
    """A stable id for a sequence, used as the procedure's claim_key so re-learning updates it."""
    return hashlib.sha256("|".join(steps).encode("utf-8")).hexdigest()[:16]
