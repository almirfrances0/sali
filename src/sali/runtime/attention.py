"""Attention classification (Prompt 1) — deterministic routing of an incoming message when Sali may
already have a primary task in focus.

Human-like focus: Sali holds ONE primary task, but Almir can reach it any time. This classifier decides —
without the LLM — whether a new message is a passing question (answer, don't disturb the task), a quick
independent action (do it, then resume), an interruption (suspend the task, handle it, resume), an explicit
replacement/cancellation of the task, or something to queue for later. It builds on TaskAuthority's existing
task-control signals and adds the finer attention categories the runtime routes on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sali.tasks.authority import _detect_cancellation, _detect_replacement, _is_clearly_new_task


class AttentionCategory(StrEnum):
    CONVERSATION = "conversation"                  # answer from knowledge; do NOT touch the primary task
    QUICK_ANSWER = "quick_answer"                  # a short factual answer (maybe a cheap read-only probe)
    QUICK_ACTION = "quick_action"                  # a short independent action, then resume the primary
    INTERRUPT_TASK = "interrupt_task"              # suspend the primary, handle this, then resume it
    REPLACE_PRIMARY_TASK = "replace_primary_task"  # explicit NEW primary — supersede the old one
    CANCEL_PRIMARY_TASK = "cancel_primary_task"    # explicit stop of the primary
    QUEUE_FOR_LATER = "queue_for_later"            # store; act after the primary finishes
    CONTINUE_PRIMARY = "continue_primary"          # refine/continue the active task (or start one if none)


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


_RANK = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2, Priority.URGENT: 3}


def _at_least(p: Priority, floor: Priority) -> Priority:
    return p if _RANK[p] >= _RANK[floor] else floor


_URGENT = re.compile(r"\b(urgent|asap|emergency|right\s+now|immediately|critical)\b", re.IGNORECASE)
_HIGH = re.compile(
    r"\b(stop\s+what\s+you'?re\s+doing|i\s+need\s+this\s+now|need\s+it\s+now|quickly|real\s+quick"
    r"|right\s+away|drop\s+everything|now\s+please)\b", re.IGNORECASE)
_LOW = re.compile(
    r"\b(later|when\s+you\s+(?:have\s+time|get\s+a\s+chance|can)|no\s+rush|whenever|eventually|no\s+hurry)\b",
    re.IGNORECASE)

_QUEUE = re.compile(
    r"\b(after\s+you\s+(?:finish|are\s+done|'?re\s+done)|when\s+you'?re\s+done|when\s+you\s+finish"
    r"|once\s+(?:you'?re|you\s+are|this\s+is|it'?s)\s+(?:done|finished)|remind\s+me|then\s+afterwards)\b",
    re.IGNORECASE)
_INTERRUPT = re.compile(
    r"\b(stop\s+for\s+a\s+(?:moment|sec|second|minute)|pause\s+(?:for|a\s+moment|what\s+you)"
    r"|hold\s+on|one\s+sec|before\s+you\s+continue|quick\s+interruption|just\s+for\s+a\s+(?:sec|moment)"
    r"|hang\s+on|take\s+a\s+break\s+from)\b", re.IGNORECASE)
# a quick independent action — a concrete verb on a concrete target
_QUICK_ACTION = re.compile(
    r"\b(zip|unzip|compress|archive|send|read|show|display|list|open|download|upload|move\s+the|copy\s+the"
    r"|rename|delete\s+the|cat|print|check\s+(?:the|this|if|why|whether)|look\s+at|find\s+(?:the|a|my)"
    r"|grep|tail|head)\b", re.IGNORECASE)
# pure status/conversation about the work
_STATUS_Q = re.compile(
    r"\b(what\s+(?:are|were)\s+you\s+(?:doing|working)|what\s+did\s+you\s+(?:just\s+)?do"
    r"|what'?s\s+(?:the\s+)?status|where\s+are\s+you\s+(?:at|with|now)|how'?s\s+it\s+going"
    r"|are\s+you\s+(?:done|finished)|current\s+(?:task|status)|what\s+task\s+are\s+you)\b", re.IGNORECASE)
_QUESTION_START = re.compile(
    r"^\s*(what|why|how|when|where|which|who|is|are|was|were|does|did|can|could|should|do\s+you\s+know)\b",
    re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class AttentionDecision:
    category: AttentionCategory
    priority: Priority
    reason: str

    @property
    def touches_primary(self) -> bool:
        """Does acting on this message change the primary task's state (suspend/replace/cancel)?"""
        return self.category in (
            AttentionCategory.INTERRUPT_TASK,
            AttentionCategory.REPLACE_PRIMARY_TASK,
            AttentionCategory.CANCEL_PRIMARY_TASK,
        )


def _priority(text: str) -> Priority:
    if _URGENT.search(text):
        return Priority.URGENT
    if _HIGH.search(text):
        return Priority.HIGH
    if _LOW.search(text):
        return Priority.LOW
    return Priority.NORMAL


def classify(message: str, *, has_primary: bool = False, current_objective: str = "") -> AttentionDecision:
    """Deterministically classify a message into an attention category + priority. Never calls the LLM."""
    text = (message or "").strip()
    prio = _priority(text)
    if not text:
        return AttentionDecision(AttentionCategory.CONVERSATION, prio, "empty message")

    # Explicit "do it later" wins regardless of task state.
    if _QUEUE.search(text):
        return AttentionDecision(AttentionCategory.QUEUE_FOR_LATER, prio, "explicit defer language")

    # Explicit task control (deterministic signals shared with TaskAuthority).
    if _detect_cancellation(text):
        return AttentionDecision(AttentionCategory.CANCEL_PRIMARY_TASK, _at_least(prio, Priority.HIGH), "explicit cancel")

    if has_primary and (_detect_replacement(text) or _is_clearly_new_task(text, current_objective)):
        return AttentionDecision(AttentionCategory.REPLACE_PRIMARY_TASK, prio, "explicit new primary task")

    # No primary task in focus: a substantive request becomes the focus; a bare question is just conversation.
    if not has_primary:
        if _STATUS_Q.search(text) or _QUESTION_START.match(text):
            return AttentionDecision(AttentionCategory.QUICK_ANSWER, prio, "question, no active task")
        return AttentionDecision(AttentionCategory.CONTINUE_PRIMARY, prio, "new request becomes the focus")

    # --- a primary task IS active ---
    if _STATUS_Q.search(text):
        return AttentionDecision(AttentionCategory.CONVERSATION, prio, "status question about current work")

    if _INTERRUPT.search(text):
        return AttentionDecision(AttentionCategory.INTERRUPT_TASK, _at_least(prio, Priority.HIGH), "explicit interrupt")

    if prio in (Priority.HIGH, Priority.URGENT) and _QUICK_ACTION.search(text):
        return AttentionDecision(AttentionCategory.INTERRUPT_TASK, prio, "urgent action while working")

    if _QUICK_ACTION.search(text):
        return AttentionDecision(AttentionCategory.QUICK_ACTION, prio, "quick independent action")

    if _QUESTION_START.match(text):
        return AttentionDecision(AttentionCategory.QUICK_ANSWER, prio, "question while working")

    return AttentionDecision(AttentionCategory.CONTINUE_PRIMARY, prio, "continuation of active task")


async def attention_snapshot(pool: Any) -> dict[str, Any]:
    """Deterministic recovery snapshot (§14): from DURABLE state alone — never the LLM's last response —
    what is Sali attending to? The primary task (if any), whether it is suspended (an interrupt was in
    flight), and how many messages are queued. Used after a crash/restart to decide what to resume."""
    async with pool.acquire() as conn:
        primary = await conn.fetchrow(
            "SELECT id, objective, status, interrupted_at, workspace_root FROM task "
            "WHERE is_primary AND status NOT IN ('done','failed','abandoned','cancelled','superseded') "
            "ORDER BY updated_at DESC LIMIT 1")
        pending = await conn.fetchval("SELECT count(*) FROM incoming_message WHERE status='pending'")
        processing = await conn.fetchval("SELECT count(*) FROM incoming_message WHERE status='processing'")
    suspended = bool(primary and primary["status"] == "paused")
    return {
        "has_primary": primary is not None,
        "primary_task_id": str(primary["id"]) if primary else None,
        "primary_objective": primary["objective"] if primary else None,
        "primary_status": primary["status"] if primary else None,
        "primary_suspended": suspended,
        "interrupt_active": bool(suspended and primary["interrupted_at"] is not None),
        "pending_messages": int(pending or 0),
        "processing_messages": int(processing or 0),
        # A suspended primary means an interrupt paused it; after recovery it should resume.
        "should_resume_primary": suspended,
    }


async def resume_context(pool: Any, task_id: Any) -> str | None:
    """Assemble the primary-task continuation block from DURABLE records (§4) — objective, workspace,
    completed/failed/next steps, last artifacts, last successful tool execution, and the interrupt that
    occurred. NOT reconstructed from chat history. Returns None if the task no longer exists."""
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM task WHERE id = $1", task_id)
        if t is None:
            return None
        steps = await conn.fetch("SELECT * FROM task_step WHERE task_id=$1 ORDER BY seq", task_id)
        artifacts = await conn.fetch(
            "SELECT artifact_path, artifact_type FROM task_artifact WHERE task_id=$1 "
            "ORDER BY created_at DESC LIMIT 5", task_id)
        last_exec = await conn.fetchrow(
            "SELECT tool_name, step_seq, result_summary FROM task_execution "
            "WHERE task_id=$1 AND status='completed' ORDER BY finished_at DESC LIMIT 1", task_id)

    lines = [f"RESUMING PRIMARY TASK: {t['objective']}", f"TASK ID: {task_id}", f"STATUS: {t['status']}"]
    if t["workspace_root"]:
        lines.append(f"WORKSPACE: {t['workspace_root']} (all writes resolve here)")
    done = [s for s in steps if s["status"] in ("done", "skipped")]
    failed = [s for s in steps if s["status"] in ("failed", "blocked")]
    nxt = next((s for s in steps if s["status"] in ("pending", "running")), None)
    if done:
        lines.append("COMPLETED STEPS: " + "; ".join(f"{s['seq']}. {s['description']}" for s in done))
    if failed:
        lines.append("FAILED/BLOCKED STEPS: " + "; ".join(f"{s['seq']}. {s['description']}" for s in failed))
    if nxt:
        lines.append(f"NEXT ACTION: step {nxt['seq']} — {nxt['description']}")
        if nxt["checkpoint"]:
            lines.append(f"RESUME MID-STEP FROM: {nxt['checkpoint']}")
    if last_exec:
        lines.append(f"LAST SUCCESSFUL TOOL: {last_exec['tool_name']} (step {last_exec['step_seq']})")
    if artifacts:
        lines.append("LAST ARTIFACTS: " + "; ".join(f"{a['artifact_type']} {a['artifact_path']}" for a in artifacts))
    if t["recovery_reason"]:
        lines.append(f"INTERRUPT THAT OCCURRED: {t['recovery_reason']}")
    lines.append("Continue from NEXT ACTION — do not restart or re-review the whole project.")
    return "\n".join(lines)
