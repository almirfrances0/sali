"""CognitiveState (Prompt: Cognitive OS §3/§4/§46) — the unified, DERIVED snapshot of everything Sali
is right now, assembled from authoritative durable sources. It is NOT another source of truth: destroy
the in-memory CognitiveState and it reconstructs entirely from PostgreSQL. It answers, in one place:
who am I, who am I talking to, what's happening, what am I doing, where, what's done/verified/failed,
what's next, what knowledge applies, what's pending, and what I can do.

Every field comes from an existing owner — identity from self-state, attention from the inbox/attention
snapshot, task/plan/workspace from TaskStore, continuation from the capsule, reviewer from TaskReviewer,
research/skills from their stores. This module only ASSEMBLES; it owns nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from sali.runtime import attention as attn
from sali.runtime.capsule import CONTEXT_VERSION, build_capsule, render_capsule

# A compact capability registry (§41): the model gets a short description of what Sali can DO, not the
# full schema of every tool (schemas are advertised/loaded per turn). Tools map into these buckets.
_CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "filesystem": ("create_file", "modify_file", "delete_file", "read_file", "list_dir"),
    "shell": ("execute_command",),
    "git": ("git_pull", "git_status", "git_commit"),
    "web_research": ("web_search", "web_fetch", "research_task", "browser_open"),
    "archives": ("zip", "unzip"),
    "memory": ("remember", "recall", "forget", "memory_search"),
    "graph": ("relate", "entity"),
    "tasks": ("plan_task", "advance_task", "finish_task", "review_task"),
    "planning": ("record_decision", "start_phase", "complete_phase"),
    "vision": ("see_screen",),
    "perception": ("perceive",),
    "scheduling": ("schedule", "unschedule"),
    "messaging": ("send_email", "notify", "send_message"),
    # NOTE: no "delegation" capability — Sali is a single executive agent with no subagents (Prompt 12 §7).
    "clarification": ("ask_user",),
}


def capability_registry(registry: Any) -> list[dict[str, Any]]:
    """A compact list of Sali's capability groups + which of them are actually available right now,
    derived from the live tool registry (§41/§42). No massive tool docs — just what Sali can do."""
    available: set[str] = set()
    try:
        available = {t.name for t in registry.advertise()}
    except Exception:  # noqa: BLE001
        available = set()
    out: list[dict[str, Any]] = []
    for group, names in _CAPABILITY_GROUPS.items():
        present = [n for n in names if n in available] if available else list(names)
        if present:
            out.append({"capability": group, "tools": present})
    return out


async def capability_overview(pool: Any, registry: Any) -> dict[str, Any]:
    """A self-describing view of what Sali can do (§34): the built-in tool-capability groups (only those
    whose tools are actually registered) MERGED with the learned, evidence-backed capabilities from
    durable state (their real status + confidence). No fake advertising — nothing claims a capability it
    doesn't have."""
    from sali.learning.capability import CapabilityStore

    builtin = capability_registry(registry)
    for g in builtin:
        g["kind"], g["status"] = "builtin", "available"
    learned = [{"capability": c["name"], "kind": "learned", "status": c["status"],
                "confidence": c["confidence"], "scope_ref": c.get("scope_ref")}
               for c in await CapabilityStore(pool).list(limit=100)]
    return {"builtin": builtin, "learned": learned}


@dataclass(slots=True)
class CognitiveState:
    session_id: str | None = None
    context_version: int = CONTEXT_VERSION
    identity: dict[str, Any] = field(default_factory=dict)
    attention: dict[str, Any] = field(default_factory=dict)
    primary_task: dict[str, Any] | None = None
    foreground_run: dict[str, Any] | None = None
    workspace: str | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    current_step: dict[str, Any] | None = None
    next_action: str = ""
    continuation: str = ""
    selected_skills: list[str] = field(default_factory=list)
    research_count: int = 0
    reviewer: dict[str, Any] | None = None
    pending_messages: list[dict[str, Any]] = field(default_factory=list)
    active_tool: str | None = None
    waiting_for_user: dict[str, Any] | None = None
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    # adaptive learning (Prompt 7 §32) — compact operational learning state, bounded
    active_learning_count: int = 0
    pending_behavior_proposals: int = 0
    recent_learning: list[dict[str, Any]] = field(default_factory=list)
    open_contradictions: int = 0
    learning_health: dict[str, Any] = field(default_factory=dict)
    # persistent agency / life loop (§3/§42) — the unified DERIVED life-state projection, bounded
    life_mode: str = "idle"
    current_activity: dict[str, Any] | None = None
    blocked_reason: str | None = None
    waiting_reason: str | None = None
    capability_count: int = 0
    recent_experience_count: int = 0
    unfinished_side_effects: int = 0
    cleanup_pending: int = 0
    # capability evolution / real-world action (Prompt 8 §55) — commitments + acquisition + externals
    current_commitment: str | None = None
    open_commitments: int = 0
    capability_acquisitions: int = 0
    external_entities: int = 0
    # digital life (Prompt 9 §43) — objects Sali owns/maintains, unfinished business, promises
    digital_objects: int = 0
    open_obligations: int = 0
    commitments: int = 0
    # autonomous life (Prompt 10 §78) — goals, initiative, routines, when Sali next wakes
    active_goals: int = 0
    open_initiatives: int = 0
    routines_due: int = 0
    known_people: int = 0
    next_wakeup: str | None = None
    # human interaction & natural consent (Prompt 11 §52/§68) — what Sali is waiting on the user for
    pending_consent: dict[str, Any] | None = None
    # async life & behavior (Prompt: human-like ongoing life §20) — waiting pauses activities, not Sali
    open_conversations: int = 0
    pending_questions: int = 0
    # intent revocation & self-preservation (Prompt 12 §27) — durable counts; live resource state via API
    revoked_intents: int = 0
    open_resource_incidents: int = 0

    def snapshot(self) -> dict[str, Any]:
        """A compact operational snapshot for observability / the iPhone controller (§46) — never any
        chain-of-thought, only durable operational state."""
        pt = self.primary_task or {}
        return {
            "session_id": self.session_id,
            "context_version": self.context_version,
            "task_id": pt.get("id"),
            "objective": pt.get("objective"),
            "task_status": pt.get("status"),
            "workspace": self.workspace,
            "phase": pt.get("phase"),
            "current_step": self.current_step.get("seq") if self.current_step else None,
            "next_action": self.next_action,
            "selected_skills": self.selected_skills,
            "research_count": self.research_count,
            "review_status": (self.reviewer or {}).get("status"),
            "pending_messages": len(self.pending_messages),
            "active_tool": self.active_tool,
            "foreground_busy": bool(self.foreground_run and self.foreground_run.get("busy")),
            "waiting_for_user": self.waiting_for_user,
            "capabilities": [c["capability"] for c in self.capabilities],
            "active_learning": self.active_learning_count,
            "pending_behavior_proposals": self.pending_behavior_proposals,
            "recent_learning": self.recent_learning,
            "open_contradictions": self.open_contradictions,
            "learning_health": self.learning_health,
            "life_mode": self.life_mode,
            "current_activity": self.current_activity,
            "blocked_reason": self.blocked_reason,
            "waiting_reason": self.waiting_reason,
            "capability_count": self.capability_count,
            "recent_experience_count": self.recent_experience_count,
            "unfinished_side_effects": self.unfinished_side_effects,
            "cleanup_pending": self.cleanup_pending,
            "current_commitment": self.current_commitment,
            "open_commitments": self.open_commitments,
            "capability_acquisitions": self.capability_acquisitions,
            "external_entities": self.external_entities,
            "digital_objects": self.digital_objects,
            "open_obligations": self.open_obligations,
            "commitments": self.commitments,
            "active_goals": self.active_goals,
            "open_initiatives": self.open_initiatives,
            "routines_due": self.routines_due,
            "known_people": self.known_people,
            "next_wakeup": self.next_wakeup,
            "pending_consent": self.pending_consent,
            "open_conversations": self.open_conversations,
            "pending_questions": self.pending_questions,
            "revoked_intents": self.revoked_intents,
            "open_resource_incidents": self.open_resource_incidents,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def assemble(
    pool: Any, *, loop: Any = None, coordinator: Any = None, lease: Any = None,
    inbox: Any = None, session_id: UUID | None = None,
) -> CognitiveState:
    """DERIVE the full CognitiveState from durable sources (§4). Best-effort per source — a missing or
    failing owner never breaks assembly; that field is simply omitted. Fully reconstructable from the DB
    (nothing here reads process RAM), so after a restart the same state comes back."""
    state = CognitiveState(session_id=str(session_id) if session_id else None)

    with _guard():
        if loop is not None:
            state.identity = await loop._self_state.assemble()
    with _guard():
        state.attention = await attn.attention_snapshot(pool)

    primary = None
    if loop is not None:
        with _guard():
            primary = await loop._tasks.active_task()

    if coordinator is not None:
        with _guard():
            cur = coordinator.current_execution
            state.foreground_run = {
                "run_id": str(cur.run_id) if cur else None,
                "origin": cur.origin.value if cur else None,
                "busy": bool(getattr(coordinator, "is_busy", False)),
            }
    with _guard():
        if loop is not None:
            state.capabilities = capability_registry(loop.registry)
    if inbox is not None:
        with _guard():
            msgs = await inbox.pending()
            state.pending_messages = [
                {"id": str(m.id), "content": (m.content or "")[:200], "priority": m.priority}
                for m in msgs]
    with _guard():
        state.recent_events = await _recent_events(pool)
    with _guard():
        ls = await _learning_snapshot(pool)
        state.active_learning_count = ls["active"]
        state.pending_behavior_proposals = ls["behavior_pending"]
        state.recent_learning = ls["recent"]
        state.open_contradictions = ls["open_contradictions"]
        state.learning_health = ls["health"]

    if primary is not None:
        state.primary_task = {
            "id": str(primary.id), "objective": primary.objective, "status": primary.status,
            "is_primary": primary.is_primary,
        }
        state.workspace = primary.workspace_root
        state.active_tool = primary.active_tool_name
        state.plan = [{"seq": s.seq, "description": s.description, "status": s.status,
                       "verified": s.verified} for s in primary.steps]
        nxt = primary.next_step
        state.current_step = {"seq": nxt.seq, "description": nxt.description} if nxt else None
        with _guard():
            cap = await build_capsule(
                pool, primary, reviewer=getattr(loop, "_reviewer", None),
                research=getattr(loop, "_research_store", None), skills=getattr(loop, "_skills", None),
                decisions=getattr(loop, "_decisions", None), phases=getattr(loop, "_phases", None))
            state.next_action = cap.next_action
            state.continuation = render_capsule(cap)
            state.primary_task["phase"] = cap.phase["name"] if cap.phase else None
            state.selected_skills = cap.skills
            state.research_count = len(cap.research)
            state.reviewer = cap.review
        with _guard():
            state.waiting_for_user = await _pending_question(pool, primary.id)
        with _guard():
            from sali.tasks.consent import ConsentStore
            state.pending_consent = await ConsentStore(pool).pending(primary.id)

    # the unified life-state projection (§3): derived from the durable agency stores + the state above
    with _guard():
        life = await _life_snapshot(pool, primary_id=(primary.id if primary is not None else None))
        state.capability_count = life["capability_count"]
        state.unfinished_side_effects = life["unfinished_side_effects"]
        state.cleanup_pending = life["cleanup_pending"]
        state.current_activity = life["current_activity"]
        state.capability_acquisitions = life["capability_acquisitions"]
        state.external_entities = life["external_entities"]
        state.digital_objects = life["digital_objects"]
        state.open_obligations = life["open_obligations"]
        state.commitments = life["commitments"]
        state.active_goals = life["active_goals"]
        state.open_initiatives = life["open_initiatives"]
        state.routines_due = life["routines_due"]
        state.known_people = life["known_people"]
        state.next_wakeup = life["next_wakeup"]
        state.open_conversations = life["open_conversations"]
        state.pending_questions = life["pending_questions"]
        state.revoked_intents = life["revoked_intents"]
        state.open_resource_incidents = life["open_resource_incidents"]
        state.current_commitment = (primary.objective if primary is not None else None)
        # the aggregate of everything still open — Sali's total unfinished operational load (§43)
        state.open_commitments = (life["unfinished_side_effects"] + life["capability_acquisitions"]
                                  + life["external_entities"] + life["cleanup_pending"]
                                  + life["open_obligations"] + life["commitments"])
    state.recent_experience_count = int(state.learning_health.get("experiences", 0))
    if state.current_activity and state.current_activity.get("status") == "blocked":
        state.blocked_reason = state.current_activity.get("error")
    if state.waiting_for_user:
        state.waiting_reason = state.waiting_for_user.get("question")
    state.life_mode = _derive_life_mode(state)
    return state


def _derive_life_mode(state: CognitiveState) -> str:
    """Sali's current operational mode, DERIVED from durable state (§3) — never the model's say-so."""
    if state.waiting_for_user:
        return "waiting"
    pt = state.primary_task or {}
    status = pt.get("status")
    if status == "waiting_for_user":
        return "waiting"
    if status in ("paused", "blocked", "interrupted"):
        return "interrupted"
    if state.foreground_run and state.foreground_run.get("busy"):
        return "working"
    if status == "running" or state.current_activity is not None:
        return "working"
    return "idle"


async def _life_snapshot(pool: Any, *, primary_id: UUID | None) -> dict[str, Any]:
    """Compact life-loop counts + the current activity, derived from the agency stores (§3/§42).
    Bounded — never dumps the ledgers into context."""
    from sali.learning.capability import CapabilityStore
    from sali.learning.capability_acquisition import CapabilityAcquisitionStore
    from sali.runtime.initiative import InitiativeEngine
    from sali.tasks.activity import ActivityStore
    from sali.tasks.cleanup import WorkspaceCleanupStore
    from sali.tasks.commitments import CommitmentStore, ObligationStore
    from sali.tasks.external import ExternalEntityStore
    from sali.tasks.goals import GoalStore
    from sali.tasks.initiatives import InitiativeStore
    from sali.tasks.people import PersonStore
    from sali.tasks.routines import RoutineStore
    from sali.tasks.side_effects import SideEffectStore

    caps = await CapabilityStore(pool).counts()
    se = await SideEffectStore(pool).counts()
    cu = await WorkspaceCleanupStore(pool).counts()
    acq = await CapabilityAcquisitionStore(pool).counts()
    ext = await ExternalEntityStore(pool).counts()
    obl = await ObligationStore(pool).counts()
    com = await CommitmentStore(pool).counts()
    goals = await GoalStore(pool).counts()
    inits = await InitiativeStore(pool).counts()
    routines = await RoutineStore(pool).counts()
    people = await PersonStore(pool).counts()
    wake = await InitiativeEngine(pool).next_wake()
    from sali.tasks.conversation import ConversationThreadStore, PendingQuestionStore
    from sali.tasks.incidents import ResourceIncidentStore
    from sali.tasks.revocation import RevocationStore
    threads = await ConversationThreadStore(pool).counts()
    questions = await PendingQuestionStore(pool).counts()
    revoked = await RevocationStore(pool).counts()
    incidents = await ResourceIncidentStore(pool).counts()
    current_activity: dict[str, Any] | None = None
    if primary_id is not None:
        act = await ActivityStore(pool).current(primary_id)
        if act:
            current_activity = {"kind": act["kind"], "status": act["status"],
                                "description": (act.get("description") or "")[:120],
                                "error": act.get("error")}
    return {"capability_count": caps["usable"], "unfinished_side_effects": se["unfinished"],
            "cleanup_pending": cu["pending"], "current_activity": current_activity,
            "capability_acquisitions": acq["in_progress"], "external_entities": ext["unfinished"],
            "digital_objects": ext["total"], "open_obligations": obl["open"], "commitments": com["open"],
            "active_goals": goals["active"], "open_initiatives": inits["open"],
            "routines_due": routines["due"], "known_people": people["total"],
            "next_wakeup": wake.isoformat() if wake else None,
            "open_conversations": threads["open"], "pending_questions": questions["waiting"],
            "revoked_intents": revoked["active_tombstones"], "open_resource_incidents": incidents["open"]}


async def _learning_snapshot(pool: Any) -> dict[str, Any]:
    """Compact adaptive-learning state, derived from durable records (Prompt 7 §32). Bounded — never
    injects the learning database into the model; a handful of recent items plus health counts."""
    from sali.learning.behavior import BehaviorStore
    from sali.learning.candidates import LearningCandidateStore
    from sali.learning.experience import ExperienceStore

    lc = LearningCandidateStore(pool)
    counts = await lc.counts()
    recent = [{"lesson": (r["lesson"] or "")[:120], "state": r["verification_state"],
               "scope": r["scope"]} for r in await lc.recent(limit=5)]
    beh = await BehaviorStore(pool).counts()
    mem = await ExperienceStore(pool).counts()   # lifetime memory: layer counts + experiences
    return {
        "active": counts["active"], "open_contradictions": counts["open_contradictions"],
        "behavior_pending": beh["pending"], "recent": recent,
        "health": {"verified": counts["verified"], "promoted": counts["promoted"],
                   "rejected": counts["rejected"], "contradicted": counts["contradicted"],
                   "memory_total": mem["total"], "experiences": mem["experiences"],
                   "memory_conflicts": mem["open_conflicts"]},
    }


async def _recent_events(pool: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, created_at FROM event ORDER BY seq DESC LIMIT $1", limit)
    return [{"type": r["event_type"],
             "at": r["created_at"].isoformat() if r["created_at"] else None} for r in rows]


async def _pending_question(pool: Any, task_id: UUID) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, question FROM task_question "
            "WHERE task_id = $1 AND status = 'pending' ORDER BY created_at DESC LIMIT 1", task_id)
    return {"id": str(row["id"]), "question": row["question"]} if row else None


class _guard:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True
