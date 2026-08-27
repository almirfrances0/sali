"""Shapes for the self-learning layer (spec §17-19).

Learning here is memory & knowledge acquisition, never retraining. A ProcedureCandidate is a
recurring command sequence *before* the evidence bar is met; a LearnedProcedure is one that
cleared it and became a procedural memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class LearnedProcedure:
    name: str
    steps: list[str]
    evidence: int  # how many distinct runs it was seen in (the evidence that earned it)


@dataclass(slots=True)
class ConsolidationResult:
    """What one consolidation pass turned raw activity into (spec §19)."""

    procedures: list[LearnedProcedure] = field(default_factory=list)
    failures_recorded: int = 0
    episodes_created: int = 0
    stm_pruned: int = 0
    tool_experiences: int = 0

    @property
    def did_something(self) -> bool:
        return bool(self.procedures or self.failures_recorded or self.episodes_created
                    or self.tool_experiences)
