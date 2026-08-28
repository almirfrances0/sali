"""The continuation packet (spec §11-12).

When context is folded/compacted, Sali must continue as the SAME Sali — never a fresh agent waking up
confused. This builds a structured, machine-readable snapshot of where the work stands. The CRITICAL
state — the active task, its step ticks, the next step, and any FAILED steps (with their FailureClass
and last error, from the durable task_step columns) — is DETERMINISTIC: read from the task store, never
trusted to the model's prose. The softer context (decisions, facts, open questions) is authored by the
model in a labeled format and parsed into fields. The packet is stored as jsonb beside the running prose
summary and rendered back into the prompt on the next turn.

Duck-typed on the Task/TaskStep shape (attributes only) so this stays dependency-light.
"""

from __future__ import annotations

import re
from typing import Any

from sali.tasks.retry import retry_decision

# Labeled sections the summarizer emits; each becomes a packet `notes` field (lower-cased).
SECTIONS = ("DONE", "NEXT", "DECISIONS", "CONSTRAINTS", "FACTS", "FAILURES", "OPEN")

PACKET_INSTRUCTION = (
    "You are Sali, folding your work so far into a STRUCTURED running memory you will read to continue "
    "— not free prose. Merge with any prior summary. Emit EXACTLY these labeled lines (omit a label "
    "only when it is genuinely empty):\n"
    "DONE: <steps/actions already completed — so you never repeat them>\n"
    "NEXT: <the single next action to take>\n"
    "DECISIONS: <key choices made, and why>\n"
    "CONSTRAINTS: <limits/requirements to respect>\n"
    "FACTS: <established facts, with how you know them>\n"
    "FAILURES: <what failed and why>\n"
    "OPEN: <unresolved questions / what you're still uncertain about>\n"
    "First person, concrete, compact. Only these labeled lines, nothing else."
)

_TICK = {"done": "✓", "failed": "✗", "running": "▷", "skipped": "–", "pending": "·"}
_SECTION_RE = re.compile(r"^\s*([A-Z][A-Z ]{2,20}):\s*(.*)$")


def parse_sections(text: str) -> dict[str, str]:
    """Parse the labeled summarizer output into packet fields. Tolerant of missing/extra/wrapped lines."""
    out: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m and m.group(1).strip() in SECTIONS:
            current = m.group(1).strip().lower()
            out[current] = m.group(2).strip()
        elif current is not None and line.strip():
            out[current] = f"{out[current]} {line.strip()}".strip()
    return {k: v for k, v in out.items() if v}


def _ticks(steps: list[dict[str, Any]]) -> str:
    return " ".join(f"{s['seq']}.{_TICK.get(s['status'], '·')}" for s in steps)


def task_fields(task: Any) -> dict[str, Any]:
    """Deterministic task state for the packet — the part that must NEVER be lost to prose (§11-12).
    Failures reuse the durable task_step columns (last_error / failure_class / attempts) from Phase 0."""
    if task is None:
        return {}
    steps = [{"seq": s.seq, "description": s.description, "status": s.status} for s in task.steps]
    nxt = getattr(task, "next_step", None)
    failures = [
        {
            "seq": s.seq, "error": s.last_error, "class": s.failure_class, "attempts": s.attempts,
            "recommend": retry_decision(s.attempts, s.failure_class).value,
        }
        for s in task.steps
        if s.status == "failed" and (getattr(s, "last_error", None) or getattr(s, "failure_class", None))
    ]
    return {
        "objective": task.objective,
        "status": task.status,
        "steps": steps,
        "next": {"seq": nxt.seq, "description": nxt.description} if nxt else None,
        "step_failures": failures,
    }


def _render_task_fields(tf: dict[str, Any]) -> str:
    if not tf.get("objective"):
        return ""
    lines = [f"TASK: {tf['objective']} [{_ticks(tf.get('steps', []))}] (status: {tf.get('status', '?')})"]
    if tf.get("next"):
        lines.append(f"NEXT: step {tf['next']['seq']} — {tf['next']['description']}")
    for f in tf.get("step_failures", []):
        rec = f.get("recommend")
        tail = f" → {rec}" if rec else ""
        lines.append(
            f"FAILED step {f['seq']} ({f.get('class', '?')}, attempt {f.get('attempts', '?')}): "
            f"{f.get('error', '')}{tail}"
        )
    return "\n".join(lines)


def render_task_header(task: Any) -> str:
    """A compact, DETERMINISTIC task header to fold into the carry-forward note (or '' when no task)."""
    return _render_task_fields(task_fields(task))


def build_packet(task: Any, summary_text: str) -> dict[str, Any]:
    """Combine deterministic task state with the model's parsed sections into one machine-readable packet."""
    return {"task": task_fields(task), "notes": parse_sections(summary_text)}


def render_packet(packet: Any) -> str:
    """Render a stored packet back into a compact continuation block for the prompt's 'earlier' entry."""
    if not isinstance(packet, dict):
        return ""
    lines: list[str] = []
    header = _render_task_fields(packet.get("task") or {})
    if header:
        lines.append(header)
    notes = packet.get("notes") or {}
    for key in ("done", "decisions", "constraints", "facts", "failures", "open"):
        if notes.get(key):
            lines.append(f"{key.upper()}: {notes[key]}")
    # the model's own NEXT only when the deterministic task didn't already provide one
    if notes.get("next") and not (packet.get("task") or {}).get("next"):
        lines.append(f"NEXT: {notes['next']}")
    return "\n".join(lines)
