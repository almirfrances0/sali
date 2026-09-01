"""TaskAuthority — deterministic active-task enforcement.

The user's newest request has higher authority than old memory, retrieval, or previous task state.
This module deterministically manages which task is the single primary ACTIVE task, handles
cancellation detection, and enforces task transitions — all outside the LLM.

The LLM must NOT decide which task is authoritative. The system does.
"""

from __future__ import annotations

import re
from uuid import UUID

from sali.obs.log import get_logger
from sali.tasks.models import Task
from sali.tasks.store import TaskStore

log = get_logger("sali.task_authority")

# Explicit cancellation patterns — these MUST trigger a deterministic state transition.
_CANCEL_PATTERNS = re.compile(
    r"\b("
    r"stop\s+that|cancel\s+that|forget\s+(?:the\s+)?(?:old|previous|that)\s*(?:task)?"
    r"|don'?t\s+continue|drop\s+(?:the\s+)?(?:previous|old|that)"
    r"|leave\s+(?:that|the\s+old|the\s+previous)"
    r"|abort\s+(?:that|this|the\s+task)"
    r"|never\s*mind\s+(?:that|the\s+task|the\s+old)"
    r"|quit\s+(?:that|the\s+task)"
    r"|halt\s+(?:that|the\s+task)"
    r")\b",
    re.IGNORECASE,
)

# Explicit replacement language — strong evidence the user wants a NEW independent task.
# These are much stronger than just starting a sentence with "help me" or "fix".
_REPLACE_PATTERNS = re.compile(
    r"(?:^|\b)"
    r"(new\s+task|instead\s*(?:,|:|$)|change\s+topic"
    r"|let'?s\s+work\s+on\s+something\s+else"
    r"|now\s+(?:I\s+want\s+(?:you\s+to|to)|help\s+me\s+with\s+something)"
    r"|different\s+(?:task|thing|question|topic)"
    r"|switch(?:ing)?\s+to|moving\s+on\s+to"
    r"|forget\s+(?:all\s+)?(?:that|this|everything)\s*[.,]?\s*(?:and|now|help|can|let)"
    r")",
    re.IGNORECASE,
)

# Continuation/refinement phrases — these indicate the user is STILL on the current task,
# even if they use task-initiating words like "help me" or "fix".
_CONTINUATION_PATTERNS = re.compile(
    r"\b("
    r"also|additionally|moreover|furthermore|still|keep\s+going|continue\s+with"
    r"|can\s+you\s+also|and\s+also|and\s+then|but\s+also|while\s+you'?re\s+at\s+it"
    r"|more\s+specifically|in\s+particular|specifically|especially"
    r"|why\s+(?:is|does|did|was|were|are|the|this|that|my|your)"
    r"|how\s+(?:do|does|did|can|to|the|this|that|is|are)"
    r"|what\s+(?:is|are|was|were|does|did|the|this|that)"
    r"|when\s+(?:did|does|is|was|were|the|this|that)"
    r"|where\s+(?:is|are|was|were|does|did|the|this|that)"
    r"|which\s+(?:is|are|was|were|does|did|the|this|that)"
    r"|explain|understand|clarify|elaborate|tell\s+me\s+more"
    r")\b",
    re.IGNORECASE,
)

# Explicit resumption patterns — these request a previous task to become active again.
# Only matches when the user explicitly references a PAST/OLD task, not the current one.
_RESUME_PATTERNS = re.compile(
    r"\b("
    r"resume\s+(?:the\s+)?(?:previous|old|last|that)\s*(?:task)?"
    r"|continue\s+(?:the\s+)?(?:previous|old|last|that)\s*(?:task)?"
    r"|go\s+back\s+to\s+(?:the\s+)?"
    r"|pick\s+up\s+(?:where|the\s+old|the\s+previous)"
    r"|return\s+to\s+(?:the\s+)?(?:previous|old|that)"
    r")\b",
    re.IGNORECASE,
)


def _detect_cancellation(user_input: str) -> str | None:
    """If the user explicitly asks to cancel/stop/drop a task, return the matched phrase.
    Returns None if no cancellation intent detected."""
    m = _CANCEL_PATTERNS.search(user_input or "")
    return m.group(0) if m else None


def _detect_replacement(user_input: str) -> str | None:
    """If the user uses explicit replacement language, return the matched phrase."""
    m = _REPLACE_PATTERNS.search(user_input or "")
    return m.group(0) if m else None


def _detect_continuation(user_input: str) -> str | None:
    """If the user uses continuation/refinement language, return the matched phrase."""
    m = _CONTINUATION_PATTERNS.search(user_input or "")
    return m.group(0) if m else None


def _detect_resume(user_input: str) -> str | None:
    """If the user explicitly asks to resume a previous task, return the matched phrase."""
    m = _RESUME_PATTERNS.search(user_input or "")
    return m.group(0) if m else None


def _is_clearly_new_task(user_input: str, current_objective: str = "") -> bool:
    """Conservative heuristic: does this input look like a new independent task?

    Task-initiating words alone are NOT sufficient. The classifier considers:
    1. Explicit replacement language ("instead", "new task", "change topic") → strong signal
    2. Continuation/refinement phrases ("why is", "how do", "also", "explain") → NOT new
    3. Topic overlap with the active task → NOT new
    4. Task-initiating verbs without continuation context → weak signal, insufficient alone

    Returns True only when evidence is strong. Conservative: uncertain → False (continuation).
    """
    text = (user_input or "").strip().lower()
    if not text:
        return False

    # Explicit replacement language is the strongest signal.
    if _detect_replacement(user_input):
        return True

    # Continuation/refinement phrases are strong evidence AGAINST a new task.
    if _detect_continuation(user_input):
        return False

    # Question words at the start are almost always continuation/refinement.
    if re.match(r"^(why|how|what|when|where|which|who|is|are|was|were|does|did|can|could|should)\b", text):
        return False

    # Topic overlap: if the current task's keywords appear in the input, it's likely a continuation.
    if current_objective:
        obj_words = set(re.findall(r"\w{3,}", current_objective.lower()))
        inp_words = set(re.findall(r"\w{3,}", text))
        overlap = obj_words & inp_words
        if len(overlap) >= 1:
            return False

    # Task-initiating verbs at the start — but ONLY if no continuation signal was found
    # AND there's no topic overlap. This is intentionally conservative: a bare "fix X" when
    # the current task is about Y might be new, but "fix X" when X relates to the current task
    # is not. Since we already checked topic overlap above, reaching here means no overlap.
    starters = re.compile(
        r"^(help\s+me\s+(?:with\s+)?(?:a\s+)?(?:new|different|another)|"
        r"research\s+(?:a\s+)?(?:new|different|another)|"
        r"build\s+(?:a\s+)?(?:new|different|another)|"
        r"create\s+(?:a\s+)?(?:new|different|another)|"
        r"set\s+up\s+(?:a\s+)?(?:new|different|another)|"
        r"deploy\s+(?:a\s+)?(?:new|different|another)|"
        r"fix\s+(?:a\s+)?(?:new|different|another)|"
        r"investigate\s+(?:a\s+)?(?:new|different|another))\b",
        re.IGNORECASE,
    )
    return bool(starters.match(text))


class TaskAuthority:
    """Deterministic active-task management. All transitions happen here, outside the LLM."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def current_active(self) -> Task | None:
        """The single primary ACTIVE task."""
        return await self._store.active_task()

    async def handle_new_turn(
        self, user_input: str, *, session_id: UUID | None = None
    ) -> TaskTransition:
        """Process a new user turn and return the authoritative task transition.

        Called at the START of every agent turn, before retrieval or context assembly.
        Deterministic — no LLM involved.
        """
        current = await self._store.active_task()

        # 1. Check for explicit cancellation.
        cancel_match = _detect_cancellation(user_input)
        if cancel_match and current is not None:
            await self._store.cancel(current.id, reason=cancel_match)
            log.info("task_cancelled", task_id=str(current.id), reason=cancel_match)
            return TaskTransition(
                action=TaskAction.CANCELLED,
                previous_task=current,
                new_task=None,
                reason=cancel_match,
            )

        # 2. Check for explicit resumption of a previous task.
        resume_match = _detect_resume(user_input)
        if resume_match:
            # Find the most recently superseded/paused task.
            prev = await self._most_recent_resumable()
            if prev is not None:
                if current is not None and current.id != prev.id:
                    await self._store.supersede(current.id, prev.id, reason="user resumed previous task")
                activated = await self._store.resume(prev.id)
                log.info("task_resumed", task_id=str(prev.id), reason=resume_match)
                return TaskTransition(
                    action=TaskAction.RESUMED,
                    previous_task=current,
                    new_task=activated,
                    reason=resume_match,
                )

        # 3. No current task — nothing to manage.
        if current is None:
            return TaskTransition(
                action=TaskAction.NONE,
                previous_task=None,
                new_task=None,
                reason="no active task",
            )

        # 4. Check if this is clearly a new independent task.
        #    Pass the current objective so topic overlap can be detected.
        #    Mark as superseded (not cancelled) — preserves execution status so the task
        #    can be resumed later. Clear is_primary so it's no longer authoritative.
        if _is_clearly_new_task(user_input, current.objective):
            async with self._store.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE task SET is_primary = false, updated_at = now() "
                    "WHERE id = $1 AND status NOT IN ('done','failed','abandoned','cancelled','superseded')",
                    current.id)
            log.info("task_superseded_by_new_request",
                     task_id=str(current.id), new_request=user_input[:80])
            return TaskTransition(
                action=TaskAction.SUPERSEDED,
                previous_task=current,
                new_task=None,
                reason=f"new independent request: {user_input[:80]}",
            )

        # 5. Default: treat as continuation of the current task.
        return TaskTransition(
            action=TaskAction.CONTINUED,
            previous_task=current,
            new_task=current,
            reason="continuation of active task",
        )

    async def activate_task(self, task_id: UUID) -> Task | None:
        """Explicitly activate a task (e.g., after plan_task creates it)."""
        current = await self._store.active_task()
        if current is not None and current.id != task_id:
            await self._store.supersede(current.id, task_id, reason="new task planned")
        return await self._store.activate(task_id)

    async def _most_recent_resumable(self) -> Task | None:
        """The most recently superseded/paused task that can be resumed.

        Queries by superseded_by IS NOT NULL (not by status), because supersede() preserves
        the task's execution status — a superseded task might still be 'running' or 'waiting'.
        """
        async with self._store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM task WHERE "
                "(superseded_by IS NOT NULL OR status IN ('paused','cancelled')) "
                "AND is_primary = false "
                "ORDER BY updated_at DESC LIMIT 1")
            if row is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM task_step WHERE task_id = $1 ORDER BY seq", row["id"])
        from sali.tasks.models import row_to_task
        return row_to_task(row, steps)


class TaskAction:
    NONE = "none"
    CONTINUED = "continued"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    RESUMED = "resumed"


class TaskTransition:
    """The result of a deterministic task authority decision."""

    __slots__ = ("action", "previous_task", "new_task", "reason")

    def __init__(
        self,
        *,
        action: str,
        previous_task: Task | None,
        new_task: Task | None,
        reason: str,
    ) -> None:
        self.action = action
        self.previous_task = previous_task
        self.new_task = new_task
        self.reason = reason

    @property
    def active_task(self) -> Task | None:
        """The task that is now authoritative (after the transition)."""
        return self.new_task

    def context_block(self) -> str | None:
        """An authoritative task block for the context engine. Deterministic — not inferred."""
        task = self.new_task or self.previous_task
        if task is None:
            return None
        lines = [
            f"CURRENT PRIMARY TASK: {task.objective}",
            f"STATUS: {task.status.upper()}",
            f"TASK ID: {task.id}",
        ]
        if task.workspace_root:
            lines.append(f"WORKSPACE ROOT: {task.workspace_root}")
            lines.append(f"WRITE BOUNDARY: {task.workspace_root}")
            lines.append("RELATIVE PATHS RESOLVE FROM: " + task.workspace_root)
        if task.steps:
            nxt = task.next_step
            if nxt:
                lines.append(f"CURRENT OBJECTIVE: step {nxt.seq} — {nxt.description}")
            ticks = " ".join(f"{s.seq}.{_TICK.get(s.status, '·')}" for s in task.steps)
            lines.append(f"PROGRESS: [{ticks}]")
        if self.action == TaskAction.SUPERSEDED and self.previous_task:
            lines.append(f"PREVIOUS TASK (superseded): {self.previous_task.objective}")
        return "\n".join(lines)


_TICK = {"done": "✓", "failed": "✗", "running": "▷", "skipped": "–", "pending": "·",
         "waiting": "⋯", "blocked": "⊘"}
