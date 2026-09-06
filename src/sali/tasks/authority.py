"""TaskAuthority — deterministic active-task enforcement.

The user's newest request has higher authority than old memory, retrieval, or previous task state.
This module deterministically manages which task is the single primary ACTIVE task, handles
cancellation detection, and enforces task transitions — all outside the LLM.

The LLM must NOT decide which task is authoritative. The system does.
"""

from __future__ import annotations

import contextlib

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


# NEGATION FLIPS EVERY ONE OF THE PATTERNS ABOVE, and they are unanchored, so they matched inside the
# opposite instruction: "don't forget that" hit `forget that` and "do not cancel that task" hit
# `cancel that` — both cancelling the primary task Almir had just asked Sali to keep. This detector is
# shared by TaskAuthority and the attention router, so the guard belongs here, once.
_NEGATED_CANCEL = re.compile(
    r"\b(?:don'?t|do\s+not|never|dont|no\s+need\s+to|rather\s+than)\s+(?:you\s+)?"
    r"(?:stop|cancel|forget|drop|leave|abort|quit|halt|abandon|scrap)\b",
    re.IGNORECASE,
)


def _detect_cancellation(user_input: str) -> str | None:
    """If the user explicitly asks to cancel/stop/drop a task, return the matched phrase.
    Returns None if no cancellation intent detected."""
    text = user_input or ""
    if _NEGATED_CANCEL.search(text):
        return None
    m = _CANCEL_PATTERNS.search(text)
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




# Turn 4: §15 follow-up detection - phrases that suggest the user is refining a
# completed task rather than opening a new one. Deliberately narrow: modification
# verbs ("make/change/update"), reference words ("it/that/the"), or "also/and/but"
# combined with an imperative. Deliberately NOT here: full task-initiating phrases
# ("build me a", "research X for me") - those are new tasks.
_FOLLOWUP_HINT = re.compile(
    r"^\s*("
    r"make\s+(?:it|the|that|this)|"
    r"change\s+(?:it|the|that|this)|"
    r"update\s+(?:it|the|that|this)|"
    r"fix\s+(?:it|the|that|this)|"
    r"remove\s+(?:the|that|this)|"
    r"add\s+(?:a|an|another|one\s+more|to\s+(?:it|the|that|this))|"
    r"also(?:\s+(?:add|make|change|include|remove))?\b|"
    r"(?:and|but)\s+(?:also|then|now)\s+|"
    r"actually,?\s+(?:make|change|update)|"
    r"can\s+you\s+(?:make|change|update|remove|add)\s+(?:it|the|that|this)|"
    r"a?\s*bit\s+(?:smaller|bigger|larger|shorter|longer|wider)|"
    r"(?:smaller|bigger|larger|shorter|longer|wider)\s+(?:please|now|too)|"
    r"one\s+more\s+thing|"
    r"forgot\s+to\s+mention"
    r")",
    re.IGNORECASE,
)

# Stopwords for coarse keyword overlap when scoring followups against parent objectives.
_FOLLOWUP_STOP = frozenset({
    "the","a","an","and","or","but","of","to","for","with","in","on","at","by","from","as",
    "is","are","was","were","be","been","being","this","that","these","those","it","its","our",
    "we","us","you","your","my","me","i","he","she","they","them","up","let","lets",
    "make","change","update","fix","remove","add","also","actually","please","now","too","one",
    "smaller","bigger","larger","shorter","longer","wider",
})


def _followup_keywords(text: str) -> set[str]:
    """Content words for coarse similarity between a follow-up message and a parent objective."""
    words = [w.strip(".,;:!?()[]{}\"'`").lower() for w in (text or "").split()]
    return {w for w in words if w and len(w) >= 3 and w not in _FOLLOWUP_STOP}

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
            # Turn 2 fix: revoke_intent (not bare store.cancel) so we get the durable tombstone,
            # dependent-work propagation, and archive. Otherwise store.resume() would happily
            # revive this task on "resume the previous task", and downstream commitments/goals
            # linked to it would stay open with no bus event to notify observers.
            from sali.runtime.revocation import revoke_intent
            await revoke_intent(self._store.pool, current.id, reason=cancel_match,
                                publisher=self._store._publisher)
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

        # 3. No active primary task. Turn 4 §15: before returning NONE, check whether
        # the message is a follow-up on a recently-completed task ("make the header
        # smaller" after a finished landing page). If so, return FOLLOWUP with the
        # matched parent so the runtime can offer / auto-create a child task that
        # inherits the parent's workspace and write roots.
        if current is None:
            match = await self.detect_followup(user_input)
            if match is not None:
                parent, reason = match
                log.info("task_followup_detected", parent_task_id=str(parent.id),
                         reason=reason)
                return TaskTransition(
                    action=TaskAction.FOLLOWUP,
                    previous_task=parent,
                    new_task=None,
                    reason=f"follow-up on completed task: {reason}",
                )
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
                # Turn 2 fix: also transition status to 'superseded' so a restart's adopt-
                # orphan scan (recovery.py) cannot silently resurrect the task Almir explicitly
                # replaced. Before this, the row stayed 'running' with is_primary=false and no
                # tombstone; detect_orphaned_tasks picked it up and recover_task adopted it
                # back to primary as soon as the successor task finished.
                await conn.execute(
                    "UPDATE task SET is_primary = false, status = 'superseded', updated_at = now() "
                    "WHERE id = $1 AND status NOT IN ('done','failed','abandoned','cancelled','superseded')",
                    current.id)
            # Turn 2: emit the supersession event so the iOS Tasks view, the WebSocket
            # replay, and any observer sees the transition happen. Before this fix the
            # inline UPDATE went silent - a task went "not primary" without a single
            # event on the bus. Kept as inline (not store.supersede()) because the new
            # task hasn't been planned yet, so superseded_by must stay NULL until plan_task
            # fires and TaskAuthority.activate_task links them.
            from sali.tasks.store import _emit_task
            with contextlib.suppress(Exception):
                async with self._store.pool.acquire() as conn:
                    await _emit_task(conn, "task.superseded", current.id,
                                     {"reason": "new independent user request",
                                      "request": user_input[:120],
                                      "successor_task_id": None},
                                     publisher=self._store._publisher)
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


    async def detect_followup(self, user_input: str) -> tuple[Task, str] | None:
        """§15 follow-up detection. Only runs when there is NO active primary task; matches the
        message against recently-completed tasks and returns (parent_task, reason) if a strong
        match is found, else None.

        Two-signal gate:
          1. The message must have a follow-up phrasing hint (make it smaller / also add / change X).
          2. The message must share content keywords with a recently-completed task's objective.

        Conservative on purpose - a false positive silently rediscovers the wrong task; a false
        negative just means Almir has to be more explicit ("continue the landing-page task").
        """
        hint = _FOLLOWUP_HINT.search(user_input or "")
        if hint is None:
            return None
        recents = await self._store.recent_completed(limit=20)
        if not recents:
            return None
        target = _followup_keywords(user_input)
        # A pure hint with no content words ("also add one more") still counts - use the most
        # recent completed task as the match. A hint plus content words that overlap the objective
        # is a stronger match. Score by overlap size; break ties by recency (recents is DESC order).
        best_score = -1
        best_task = None
        for parent in recents:
            parent_kw = _followup_keywords(str(parent.objective or ""))
            overlap = len(target & parent_kw)
            # Even zero overlap wins vs -1: with only a hint we take the most-recent task (§15).
            if overlap > best_score:
                best_score = overlap
                best_task = parent
        if best_task is None:
            return None
        return (best_task, hint.group(0)[:80])

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
    FOLLOWUP = "followup"  # Turn 4: message continues from a recently-completed task


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
