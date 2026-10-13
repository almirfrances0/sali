"""Cognitive Executive — the decision-making core of Sali's mind.

The executive decides WHEN and HOW to use the LLM. It is NOT the LLM — it is the thing
that decides whether cognition is needed, what to think about, and what actions to take.

The executive receives signals from the attention system and the current cognitive state,
then produces a CognitiveDecision that the runtime loop executes.

Sali is a digital person. The executive is her capacity for deliberate thought —
the ability to decide what deserves attention rather than just responding to prompts.

Key design: cheap decisions are deterministic (no LLM). Expensive reasoning uses the LLM
through the existing provider interface. The executive decides which path to take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.executive")


class CognitiveAction(StrEnum):
    """What the executive decides Sali should do."""
    RESPOND = "respond"          # Generate a response to the user
    THINK = "think"              # Use the LLM to reason about something
    RETRIEVE = "retrieve"        # Search memory/graph for specific information
    INVESTIGATE = "investigate"  # Run an experiment or check something
    REFLECT = "reflect"          # Analyze recent experience
    LEARN = "learn"              # Extract a lesson from what just happened
    CREATE_GOAL = "create_goal"  # Generate a new goal from observation
    UPDATE_MEMORY = "update"     # Update working memory with new state
    COMMUNICATE = "communicate"  # Write to Almir proactively
    REST = "rest"                # Nothing needs attention right now


@dataclass
class CognitiveDecision:
    """What the executive decided Sali should do."""
    action: CognitiveAction
    reason: str                          # Why this action was chosen
    priority: float = 0.5                # 0.0-1.0
    target: str | None = None            # What to focus on (query, topic, entity)
    metadata: dict[str, Any] = field(default_factory=dict)  # Additional context


@dataclass
class AttentionSignal:
    """A signal from the attention system that something deserves focus."""
    source: str                          # 'user_message' | 'goal' | 'failure' | 'curiosity' | etc.
    urgency: float                       # 0.0-1.0
    importance: float                    # 0.0-1.0
    content: str                         # What the signal is about
    metadata: dict[str, Any] = field(default_factory=dict)


class CognitiveExecutive:
    """The decision-making core. Decides whether cognition is needed and what to think about.

    The executive is called BEFORE the LLM. It examines the current state and decides:
    - Is there something worth thinking about?
    - What should Sali focus on?
    - What kind of cognition is needed?

    The executive is deterministic — it doesn't call the LLM. It uses simple heuristics
    to make cheap decisions. Expensive reasoning is delegated to the LLM through the
    runtime loop after the executive decides it's needed.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def perceive(self, *, user_input: str | None = None,
                       has_active_task: bool = False,
                       tool_failures_this_turn: int = 0,
                       turn_iteration: int = 0) -> list[AttentionSignal]:
        """Gather attention signals from the current state. Cheap — no LLM call."""
        signals: list[AttentionSignal] = []

        # User message is always highest priority
        if user_input and len(user_input.strip()) > 5:
            signals.append(AttentionSignal(
                source="user_message", urgency=1.0, importance=1.0,
                content=user_input[:200]))

        # Working memory entries deserve attention
        if self._pool is not None:
            try:
                async with self._pool.acquire() as conn:
                    # Active problems in working memory
                    problems = await conn.fetch(
                        "SELECT content FROM sali.working_memory "
                        "WHERE category='active_problem' AND superseded_by IS NULL "
                        "AND (expires_at IS NULL OR expires_at > now()) LIMIT 3")
                    for p in problems:
                        signals.append(AttentionSignal(
                            source="working_memory", urgency=0.6, importance=0.7,
                            content=p["content"][:200]))

                    # Unresolved hypotheses
                    hypotheses = await conn.fetch(
                        "SELECT content FROM sali.working_memory "
                        "WHERE category='hypothesis' AND superseded_by IS NULL "
                        "AND (expires_at IS NULL OR expires_at > now()) LIMIT 3")
                    for h in hypotheses:
                        signals.append(AttentionSignal(
                            source="hypothesis", urgency=0.4, importance=0.6,
                            content=h["content"][:200]))

                    # Open curiosities
                    curiosities = await conn.fetch(
                        "SELECT subject, priority FROM sali.curiosity "
                        "WHERE status='open' ORDER BY priority DESC LIMIT 3")
                    for c in curiosities:
                        signals.append(AttentionSignal(
                            source="curiosity", urgency=float(c["priority"]),
                            importance=0.5, content=c["subject"][:200]))

                    # Overdue commitments
                    overdue = await conn.fetch(
                        "SELECT description FROM sali.commitment "
                        "WHERE status IN ('open','in_progress') AND deadline < now() "
                        "ORDER BY deadline LIMIT 3")
                    for o in overdue:
                        signals.append(AttentionSignal(
                            source="commitment", urgency=0.8, importance=0.8,
                            content=o["description"][:200]))

                    # Pending questions from Almir
                    questions = await conn.fetch(
                        "SELECT question FROM sali.pending_question "
                        "WHERE status='waiting' LIMIT 3")
                    for q in questions:
                        signals.append(AttentionSignal(
                            source="question", urgency=0.9, importance=0.9,
                            content=q["question"][:200]))

                    # Active directives — ongoing behavior directives that influence every cycle.
                    # A directive like "monitor the system" generates attention signals that drive
                    # the executive to check system health periodically.
                    directives = await conn.fetch(
                        "SELECT content FROM sali.working_memory "
                        "WHERE category='directive' AND superseded_by IS NULL "
                        "AND (expires_at IS NULL OR expires_at > now()) LIMIT 3")
                    for d in directives:
                        signals.append(AttentionSignal(
                            source="directive", urgency=0.5, importance=0.7,
                            content=d["content"][:200]))

            except Exception:  # noqa: BLE001
                pass

        # Tool failures deserve investigation
        if tool_failures_this_turn > 0:
            signals.append(AttentionSignal(
                source="failure", urgency=0.7, importance=0.6,
                content=f"{tool_failures_this_turn} tool failures this turn"))

        # Sort by combined score
        signals.sort(key=lambda s: s.urgency * 0.5 + s.importance * 0.5, reverse=True)
        return signals

    async def decide(self, signals: list[AttentionSignal], *,
                     user_message_pending: bool = False,
                     has_active_task: bool = False,
                     turn_iteration: int = 0) -> CognitiveDecision:
        """Decide what Sali should do based on attention signals. Deterministic — no LLM call.

        This is the core of the cognitive executive. It examines the signals and decides
        the most valuable cognitive action.
        """
        if not signals:
            return CognitiveDecision(
                action=CognitiveAction.REST,
                reason="nothing demands attention",
                priority=0.0)

        top = signals[0]

        # User message always gets a response
        if user_message_pending:
            return CognitiveDecision(
                action=CognitiveAction.RESPOND,
                reason=f"Almir said: {top.content[:60]}",
                priority=1.0,
                target=top.content[:200])

        # Pending questions from Almir
        if top.source == "question":
            return CognitiveDecision(
                action=CognitiveAction.COMMUNICATE,
                reason=f"Almir asked: {top.content[:60]}",
                priority=0.9,
                target=top.content[:200])

        # Overdue commitments
        if top.source == "commitment":
            return CognitiveDecision(
                action=CognitiveAction.COMMUNICATE,
                reason=f"overdue commitment: {top.content[:60]}",
                priority=0.8,
                target=top.content[:200])

        # Tool failures — investigate
        if top.source == "failure":
            return CognitiveDecision(
                action=CognitiveAction.INVESTIGATE,
                reason=f"tool failures detected: {top.content[:60]}",
                priority=0.7,
                target=top.content[:200])

        # Active problems in working memory — think about them
        if top.source == "working_memory":
            return CognitiveDecision(
                action=CognitiveAction.THINK,
                reason=f"active problem: {top.content[:60]}",
                priority=0.6,
                target=top.content[:200])

        # Hypotheses need testing
        if top.source == "hypothesis":
            return CognitiveDecision(
                action=CognitiveAction.INVESTIGATE,
                reason=f"hypothesis to test: {top.content[:60]}",
                priority=0.5,
                target=top.content[:200])

        # Curiosities deserve investigation during idle time
        if top.source == "curiosity":
            return CognitiveDecision(
                action=CognitiveAction.INVESTIGATE,
                reason=f"curiosity: {top.content[:60]}",
                priority=0.3,
                target=top.content[:200])

        # Active directives — Sali should check on her standing directives
        if top.source == "directive":
            return CognitiveDecision(
                action=CognitiveAction.INVESTIGATE,
                reason=f"directive: {top.content[:60]}",
                priority=0.5,
                target=top.content[:200])

        # Default: rest
        return CognitiveDecision(
            action=CognitiveAction.REST,
            reason="no urgent signals",
            priority=0.0)
