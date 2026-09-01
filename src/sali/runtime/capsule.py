"""The Task State Capsule (Prompt 6 §11) — Sali's small working memory over a large durable mind.

Every reasoning cycle, Sali wakes up with a compact, DETERMINISTIC capsule REGENERATED from PostgreSQL
— never from the model remembering a previous context window. It answers, from durable state: who/what/
where, current phase, what's done/verified/failed, active decisions, negative knowledge (what failed,
don't repeat), the reviewer's requirements, relevant research, the last success, and — anchored
explicitly — the NEXT ACTION. Operational state is lossless because it is reconstructed from structured
storage; only historical prose is compressed elsewhere. The capsule is bounded so it fits a ~24K budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sali.runtime import continuation
from sali.tasks import history

CONTEXT_VERSION = 1


@dataclass(slots=True)
class TaskStateCapsule:
    task_id: UUID
    objective: str
    status: str
    workspace_root: str | None = None
    phase: dict[str, Any] | None = None
    phases: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, int] = field(default_factory=dict)
    step_failures: list[dict[str, Any]] = field(default_factory=list)
    next_step: dict[str, Any] | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] | None = None
    research: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    last_success: dict[str, Any] | None = None
    negatives: list[dict[str, Any]] = field(default_factory=list)
    repeated: list[dict[str, Any]] = field(default_factory=list)
    next_action: str = ""

    def source_ids(self) -> dict[str, list[str]]:
        """The durable IDs the capsule drew on — a reproducible manifest (§23/§25), not the raw context."""
        return {
            "research": [str(r.get("id")) for r in self.research if r.get("id")],
            "executions": [str(e.get("id")) for e in (self.negatives + ([self.last_success]
                           if self.last_success else [])) if e and e.get("id")],
            "decisions": [str(d.get("id")) for d in self.decisions if d.get("id")],
            "skills": list(self.skills),
        }


def _next_action(task: Any, review: dict[str, Any] | None, next_step: dict[str, Any] | None,
                 blocked: bool) -> str:
    """The single deterministic NEXT ACTION (§12) — from durable state, never inferred from history."""
    if review and review.get("status") in ("needs_rework", "blocked", "failed"):
        return ("Fix ONLY the failed/blocked requirements in the TASK REVIEW above, then run the review "
                "again. Do not restart the task or repeat verified work.")
    if next_step:
        return (f"Do step {next_step['seq']}: {next_step['description']} — inside the workspace. Continue "
                "from existing files; do not restart the project or recreate verified work.")
    if blocked:
        return "Resolve the blocking dependency, then continue with the next ready step."
    if task is not None and getattr(task, "next_step", None) is None and task.steps:
        return "All steps are done — run finish_task so the reviewer can verify and complete the task."
    return f"Continue the task: {getattr(task, 'objective', '')}."


async def build_capsule(
    pool: Any, task: Any, *, reviewer: Any = None, research: Any = None, skills: Any = None,
    decisions: Any = None, phases: Any = None,
    current_error: str | None = None, current_tool: str | None = None,
) -> TaskStateCapsule:
    """Regenerate the capsule from durable state. Every store is optional (best-effort per source), so a
    bare loop still produces a valid capsule from the task alone. Deterministic and cheap (indexed reads)."""
    tf = continuation.task_fields(task)
    nxt = getattr(task, "next_step", None)
    next_step = {"seq": nxt.seq, "description": nxt.description} if nxt else None
    step = getattr(nxt, "seq", None)

    cap = TaskStateCapsule(
        task_id=task.id, objective=task.objective, status=task.status,
        workspace_root=getattr(task, "workspace_root", None),
        steps=tf.get("steps", []), verification=tf.get("verification", {}),
        step_failures=tf.get("step_failures", []), next_step=next_step,
    )

    if decisions is not None:
        with _guard():
            cap.decisions = await decisions.active(task.id)
    if phases is not None:
        with _guard():
            cap.phase = await phases.current(task.id)
            cap.phases = await phases.phases(task.id)
    if reviewer is not None:
        with _guard():
            latest = await reviewer.latest_terminal_review(task.id)
            if latest is not None and latest.status.value in ("needs_rework", "blocked", "failed"):
                cap.review = latest.to_public()
    if research is not None:
        with _guard():
            cap.research = await research.list_research(task.id, limit=3)
    if skills is not None:
        with _guard():
            cap.skills = [r["name"] for r in await skills.for_task(task.id)]
    with _guard():
        cap.last_success = await history.last_successful_execution(pool, task.id)
    with _guard():
        cap.negatives = await history.recent_failures(pool, task.id, limit=6)
    with _guard():
        cap.repeated = await history.repeated_failures(pool, task.id, threshold=3)
    # if we know the current error/tool, surface the specifically-relevant prior executions too
    if current_error or current_tool:
        with _guard():
            extra = await history.relevant_executions(
                pool, task.id, error=current_error, tool=current_tool, step=step, limit=4)
            seen = {str(n.get("id")) for n in cap.negatives}
            cap.negatives.extend(e for e in extra if e.get("status") == "failed"
                                 and str(e.get("id")) not in seen)

    blocked = bool(getattr(task, "blocked_steps", []))
    cap.next_action = _next_action(task, cap.review, next_step, blocked)
    return cap


class _guard:
    """A tiny context manager that swallows any per-source error — a missing/failing source must never
    break capsule assembly; the capsule just omits that section (best-effort, deterministic fallback)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True  # suppress everything


_TICK = {"done": "✓", "failed": "✗", "running": "▷", "skipped": "–", "pending": "·",
         "waiting": "⋯", "blocked": "⊘"}


def render_capsule(cap: TaskStateCapsule, *, emergency: bool = False) -> str:
    """Render the capsule into a bounded, deterministic context block, NEXT ACTION anchored (§11/§12).
    ``emergency`` produces the minimal survival capsule for severe context pressure (§42)."""
    lines: list[str] = [f"=== TASK STATE CAPSULE (v{CONTEXT_VERSION}) ==="]
    lines.append(f"Task {cap.task_id} — {cap.objective}  (status: {cap.status})")
    if cap.workspace_root:
        lines.append(f"Workspace: {cap.workspace_root}")
    if cap.phase:
        lines.append(f"Phase: {cap.phase['seq']}. {cap.phase['name']}")
    ticks = " ".join(f"{s['seq']}.{_TICK.get(s['status'], '·')}" for s in cap.steps)
    if ticks:
        v = cap.verification or {}
        lines.append(
            f"Steps: [{ticks}]  VERIFIED: {v.get('verified', 0)} proven · "
            f"{v.get('attempted', 0)} done-unverified · {v.get('failed', 0)} failed · "
            f"{v.get('pending', 0) + v.get('unknown', 0)} pending")
    for f in cap.step_failures[:6]:
        rec = f.get("recommend")
        tail = f" → {rec}" if rec else ""
        lines.append(
            f"FAILED step {f['seq']} ({f.get('class', '?')}, attempt {f.get('attempts', '?')}): "
            f"{f.get('error', '')}{tail}")

    if cap.decisions:
        lines.append("DECISIONS (active — respect these):")
        for d in cap.decisions[:8]:
            reason = f" — {d['reason']}" if d.get("reason") else ""
            lines.append(f"  • {d['decision']}{reason} [{d.get('source', 'sali')}]")

    if not emergency and cap.phases:
        done_phases = [p for p in cap.phases if p.get("status") == "done" and p.get("summary")]
        if done_phases:
            lines.append("COMPLETED PHASES:")
            for p in done_phases[-5:]:
                lines.append(f"  ✓ {p['seq']}. {p['name']}: {(p['summary'] or '')[:200]}")

    if cap.negatives:
        lines.append("NEGATIVE KNOWLEDGE (already failed — do NOT repeat blindly):")
        for e in cap.negatives[:6]:
            ref = f" (execution {str(e['id'])[:8]})" if e.get("id") else ""
            lines.append(f"  ✗ {e.get('tool_name', '?')} failed: {(e.get('error') or '')[:140]}{ref}")
    if cap.repeated:
        for r in cap.repeated[:3]:
            lines.append(
                f"REPEATED FAILED ACTION: {r['tool_name']} failed {r['n']}× with "
                f"\"{(r.get('error') or '')[:100]}\" — research or try an alternative / ask, don't repeat.")

    if cap.last_success:
        ls = cap.last_success
        lines.append(f"LAST SUCCESS: {ls.get('tool_name', '?')} on step {ls.get('step_seq', '?')} — "
                     f"{(ls.get('result_summary') or 'ok')[:120]}")

    review_block = continuation.render_review_block(cap.review) if cap.review else ""
    if review_block:
        lines.append(review_block)

    if not emergency and cap.research:
        lines.append("RESEARCH FINDINGS (evidence — verify, don't treat as proof):")
        for r in cap.research[:3]:
            src = f" ({r['source']})" if r.get("source") else ""
            lines.append(f"  - {(r.get('query') or '')[:80]}: {(r.get('summary') or '')[:200]}{src}")

    if cap.workspace_root:
        # the full workspace rules block (kept for the model's guidance, from durable state §5)
        lines.append(continuation.render_workspace_block(cap.workspace_root))

    lines.append(f"NEXT ACTION\n-----------\n{cap.next_action}")
    return "\n".join(x for x in lines if x)
