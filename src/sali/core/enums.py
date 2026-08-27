"""Enumerations mirroring the Postgres ENUM types (schema `sali`).

Kept as ``(str, Enum)`` so values round-trip transparently through asyncpg. The
Python and SQL definitions must stay in lockstep; ``source_priority`` mirrors the
SQL function of the same name (evidence-priority ordering, engineering rule 6).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class MemorySource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    CONVERSATION = "conversation"
    SYSTEM_OBSERVATION = "system_observation"
    FILE_OBSERVATION = "file_observation"
    TOOL_RESULT = "tool_result"
    EXTERNAL_SOURCE = "external_source"
    INFERENCE = "inference"
    PROCEDURE_EXECUTION = "procedure_execution"


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PREFERENCE = "preference"
    SYSTEM_ENV = "system_env"
    IDENTITY = "identity"


class FreshnessPolicy(StrEnum):
    REALTIME = "realtime"
    FAST = "fast"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    SLOW = "slow"
    PERMANENT = "permanent"


class RiskLevel(IntEnum):
    R0 = 0  # read-only observation
    R1 = 1  # low-risk, scope-bounded modification
    R2 = 2  # meaningful modification / network egress
    R3 = 3  # dangerous operation
    R4 = 4  # critical / destructive


class Capability(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    SYSTEM = "system"
    DESTRUCTIVE = "destructive"


class ExecAuthority(StrEnum):
    """The execution-authority tier of a *discovered* external tool (spec §34/§35).

    It is computed OUTSIDE the model and stored immutably (`tool_authority`); the model can request
    a tool but never redefine its tier. The tier maps to a `RiskLevel`/`Capability` set (below) so
    the single `PolicyEngine.decide()` gate enforces it — this is a mapping, not a second gate.
    Kept in lockstep with the SQL CHECK on `tool_authority.authority`.
    """

    NORMAL = "normal"                    # everyday tooling — runs freely (the 'freedom' default)
    ELEVATED = "elevated"                # meaningful/dangerous — still autonomous under policy
    SYSTEM_CRITICAL = "system_critical"  # destructive/irreversible — the immutable boundary (confirm)


# Authority → declared RiskLevel. Only R4 is gated (freedom policy auto-allows R0–R3), so
# SYSTEM_CRITICAL → R4 is exactly the immutable boundary; NORMAL/ELEVATED stay autonomous. This is a
# coarse per-tool FLOOR — a wrapper's per-call assess() may still refine the risk of a given call.
_AUTHORITY_RISK: dict[ExecAuthority, RiskLevel] = {
    ExecAuthority.NORMAL: RiskLevel.R2,
    ExecAuthority.ELEVATED: RiskLevel.R3,
    ExecAuthority.SYSTEM_CRITICAL: RiskLevel.R4,
}


def authority_risk(authority: ExecAuthority) -> RiskLevel:
    """The RiskLevel the single policy gate should enforce for a tool of this authority tier."""
    return _AUTHORITY_RISK[authority]


def authority_capabilities(authority: ExecAuthority) -> frozenset[Capability]:
    """Security capabilities a tier implies. Every discovered tool can EXECUTE; SYSTEM_CRITICAL
    activates Capability.SYSTEM (the immutable-boundary anchor) plus DESTRUCTIVE; ELEVATED marks
    SYSTEM without DESTRUCTIVE. The policy's DESTRUCTIVE/SYSTEM floors then apply automatically."""
    caps = {Capability.EXECUTE}
    if authority is ExecAuthority.SYSTEM_CRITICAL:
        caps |= {Capability.SYSTEM, Capability.DESTRUCTIVE}
    elif authority is ExecAuthority.ELEVATED:
        caps |= {Capability.SYSTEM}
    return frozenset(caps)


_SOURCE_PRIORITY: dict[MemorySource, int] = {
    MemorySource.SYSTEM_OBSERVATION: 100,
    MemorySource.FILE_OBSERVATION: 100,
    MemorySource.USER_EXPLICIT: 80,
    MemorySource.TOOL_RESULT: 60,
    MemorySource.PROCEDURE_EXECUTION: 60,
    MemorySource.EXTERNAL_SOURCE: 40,
    MemorySource.CONVERSATION: 30,
    MemorySource.INFERENCE: 20,
}


def source_priority(source: MemorySource) -> int:
    """Evidence-priority rank; mirrors the SQL ``source_priority`` function."""
    return _SOURCE_PRIORITY.get(source, 0)


def compare_sources(new: MemorySource, old: MemorySource) -> str:
    """Resolve two sources by evidence priority: 'new', 'old', or 'tie' (equal priority)."""
    new_p, old_p = source_priority(new), source_priority(old)
    if new_p > old_p:
        return "new"
    if new_p < old_p:
        return "old"
    return "tie"


# Sources that come from directly INSPECTING reality (not a claim about it) — a live look.
_OBSERVED = frozenset({MemorySource.SYSTEM_OBSERVATION, MemorySource.FILE_OBSERVATION})


def is_observation(source: MemorySource) -> bool:
    """True if this source is a direct inspection of reality (vs a claim about it) — the thing that
    can VERIFY a contested fact rather than merely out-rank it by priority."""
    return source in _OBSERVED


def contradiction_lifecycle(
    old_source: MemorySource, new_source: MemorySource, *, verifiable: bool
) -> tuple[str, str]:
    """How a contradiction should be recorded (spec §20/§25 'create CONTRADICTION, then verify').

    Returns (status, resolved_by):
    - a live inspection just settled it              → ('resolved', 'verification')
    - a system-OBSERVABLE slot resolved only by      → ('open', 'evidence_priority')
      priority, and something can re-inspect it later   (a best guess, flagged to verify by re-observation)
    - not checkable against reality                  → ('resolved', 'evidence_priority')  (priority is final)
    """
    if new_source in _OBSERVED:
        return "resolved", "verification"
    if verifiable and old_source in _OBSERVED:
        return "open", "evidence_priority"
    return "resolved", "evidence_priority"
