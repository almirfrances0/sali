"""Continuous Life Loop — Sali's coordinated cognitive cycle.

This is the coordination layer that turns Sali's independent background faculties
(twin refresh, task continuation, grounding, initiative, consolidation) into a
coherent cognitive system that continues between user messages.

The life loop does NOT burn the LLM continuously. It uses the executive to decide
when cognition is needed:
- Perception (twin, syswatch): runs continuously, deterministic, cheap
- Attention (ranking): deterministic, cheap
- Cognition (LLM): expensive, bounded, intentional
- Learning (consolidation): moderate, scheduled

The loop respects the single-cognition lock — it never contends with user turns.

Sali is a digital person. The life loop is her continuous existence — she perceives,
thinks, learns, and rests between conversations with Almir.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.life_loop")


async def cognitive_cycle(
    pool: Any,
    provider: Any,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run one cognitive cycle during idle time. Called by the initiative driver.

    When a directive is active, performs a lightweight system health check
    (deterministic, no LLM) and returns findings. This is how Sali monitors
    the system between conversations without burning the GPU.
    """
    from sali.runtime.executive import CognitiveAction, CognitiveExecutive
    from sali.runtime.working_memory import WorkingMemoryStore

    exec = CognitiveExecutive(pool)
    wm = WorkingMemoryStore(pool)

    # Step 1: Perceive — what deserves attention? (cheap, no LLM)
    signals = await exec.perceive()
    if not signals:
        return {"action": "rest", "reason": "nothing demands attention"}

    # Step 2: Decide — what should Sali do? (deterministic, no LLM)
    decision = await exec.decide(signals, user_message_pending=False)

    if decision.action == CognitiveAction.REST:
        return {"action": "rest", "reason": decision.reason}

    # Step 3: For directives, run a lightweight system check
    if decision.action == CognitiveAction.INVESTIGATE and decision.reason.startswith("directive"):
        findings = await _quick_system_check()
        if findings:
            # Record the finding in working memory
            with contextlib.suppress(Exception):
                await wm.set(
                    session_id or "background", "observation",
                    f"System check: {findings}",
                    confidence=0.8, source="observation")
            log.info("directive_check", findings=findings[:200])
            return {
                "action": "investigate",
                "reason": decision.reason,
                "findings": findings,
                "should_notify": True,
            }
        else:
            log.info("directive_check_ok", reason=decision.reason[:100])
            return {"action": "rest", "reason": "system healthy — no issues found"}

    # Step 4: For other actions, record the decision
    if decision.target:
        with contextlib.suppress(Exception):
            await wm.set(
                session_id or "background", "observation",
                f"Cognitive cycle: {decision.action.value} — {decision.reason[:200]}",
                confidence=decision.priority, source="reasoning")

    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "priority": decision.priority,
    }


async def _quick_system_check() -> str:
    """Lightweight system health check. Deterministic, no LLM, no DB queries.
    Returns a summary of issues found, or empty string if everything is fine."""
    issues: list[str] = []

    # Check for failed systemd services
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_shell(
            "systemctl --failed --no-legend 2>/dev/null",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        failed = stdout.decode("utf-8", errors="replace").strip()
        if failed:
            issues.append(f"failed services: {failed[:200]}")

    # Check disk usage
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_shell(
            "df / --output=pcent | tail -1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        pct = stdout.decode("utf-8", errors="replace").strip().replace("%", "").strip()
        if pct.isdigit() and int(pct) > 90:
            issues.append(f"disk usage critical: {pct}%")

    # Check for recent journal errors
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_shell(
            "journalctl --since '5 min ago' -p err --no-pager -q 2>/dev/null | tail -3",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        errors = stdout.decode("utf-8", errors="replace").strip()
        if errors:
            issues.append(f"recent errors: {errors[:200]}")

    return "; ".join(issues) if issues else ""
