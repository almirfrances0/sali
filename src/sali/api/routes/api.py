"""REST API routes — the HTTP contract for mobile clients.

All message submissions go through the AgentRuntimeCoordinator,
which serializes execution and prevents concurrent agent loops.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from sali.api.auth import Identity, require_controller, require_identity
from sali.api.models import (
    ConversationMessage,
    ConversationState,
    HealthResponse,
    ScheduleResponse,
    SendMessageRequest,
    SendMessageResponse,
    SystemStatus,
    TaskListResponse,
    TaskResponse,
    TaskStepResponse,
)

# Every route on this router requires an authenticated identity (device or host) — unauthenticated access
# is rejected with 401 (§43). Mutating routes additionally depend on `require_controller` (observers are
# read-only, §27). Enrollment/refresh live on a separate unauthenticated router (routes/enroll.py).
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_identity)])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kernel(request: Request) -> Any:
    return request.app.state.kernel


def _runtime(request: Request) -> Any:
    return request.app.state.runtime


def _publisher(request: Request) -> Any:
    """The canonical EventPublisher, for routes that emit events (e.g. approving a behavior proposal)."""
    return getattr(request.app.state.runtime, "_publisher", None)


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """System health — API server + subsystem checks."""
    kernel = _kernel(request)
    try:
        from sali.runtime.health import HealthService
        pool = await kernel.pool()
        svc = HealthService(pool, kernel.provider)
        h = await svc.check()
        return HealthResponse(
            status="ok" if h.all_ok else "degraded",
            datastore=h.datastore,
            model=h.model,
            embedder=h.embedder,
            internet=h.internet,
            detail=h.detail,
        )
    except Exception as exc:
        return HealthResponse(status="error", detail={"error": str(exc)[:200]})


# ── System Status ─────────────────────────────────────────────────────────────

@router.get("/status", response_model=SystemStatus)
async def status(request: Request) -> SystemStatus:
    """Current system status including execution state."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali import __version__
    from sali.runtime.session import persistent_session_id
    from sali.tasks.store import TaskStore

    store = TaskStore(pool)
    active = await store.active_task()

    try:
        async with pool.acquire() as conn:
            mem_count = await conn.fetchval(
                "SELECT count(*) FROM memory WHERE valid_until IS NULL")
    except Exception:
        mem_count = None

    return SystemStatus(
        version=__version__,
        model=kernel.settings.model.chat_model,
        session_id=str(persistent_session_id()),
        active_task_id=active.id if active else None,
        memory_count=mem_count,
    )


# ── Conversation ──────────────────────────────────────────────────────────────

@router.get("/conversation", response_model=ConversationState)
async def get_conversation(request: Request, limit: int = 50) -> ConversationState:
    """Get the current conversation state (shared across all interfaces)."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.runtime.session import persistent_session_id

    session_id = persistent_session_id()
    async with pool.acquire() as conn:
        conv = await conn.fetchrow(
            "SELECT summary, last_active FROM conversation WHERE id = $1", session_id)
        rows = await conn.fetch(
            "SELECT conversation_id, seq, role, content, model, created_at "
            "FROM message WHERE conversation_id = $1 ORDER BY seq DESC LIMIT $2",
            session_id, limit)

    messages = [
        ConversationMessage(
            id=session_id,
            session_id=r["conversation_id"],
            seq=r["seq"],
            role=r["role"],
            content=r["content"],
            model=r["model"],
            created_at=r["created_at"],
        )
        for r in reversed(rows)
    ]

    return ConversationState(
        session_id=session_id,
        messages=messages,
        summary=conv["summary"] if conv else None,
        last_active=conv["last_active"] if conv else None,
    )


@router.post("/conversation/message", response_model=SendMessageResponse)
async def send_message(
    body: SendMessageRequest, request: Request, _: Identity = Depends(require_controller),
) -> SendMessageResponse:
    """Send a message to Sali. Serialized through the runtime.

    If another turn is running (terminal or API), this message is queued.
    The response streams via WebSocket.
    """
    runtime = _runtime(request)

    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    import asyncio

    async def _submit() -> None:
        try:
            # Route through the attention authority (Prompt 2) — same semantics as terminal + iOS. The turn's
            # completion is signalled by the canonical, durable `agent.final` event the coordinator already
            # publishes (which replays on reconnect); we no longer emit a redundant, non-durable,
            # sequence-less `message.completed` broadcast that the recovery contract couldn't replay
            # (Final audit §29/§31).
            await runtime.handle_message(body.content, origin="api")
        except Exception as exc:
            from sali.api.ws import manager as ws_manager
            await ws_manager.broadcast_event("error", {"error": str(exc)[:200]})

    asyncio.create_task(_submit())

    from sali.core.ids import new_id
    return SendMessageResponse(run_id=new_id(), status="accepted")


@router.post("/conversation/cancel")
async def cancel_execution(request: Request, _: Identity = Depends(require_controller)) -> dict[str, Any]:
    """Cancel the currently running foreground execution."""
    runtime = _runtime(request)
    cancelled = await runtime.cancel_foreground()
    return {"cancelled": cancelled, "was_busy": runtime.is_busy}


# ── Tasks ─────────────────────────────────────────────────────────────────────

@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(request: Request) -> TaskListResponse:
    """List all open/active tasks."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.store import TaskStore

    store = TaskStore(pool)
    tasks = await store.open_tasks(limit=20)
    active = await store.active_task()

    return TaskListResponse(
        tasks=[_task_to_response(t) for t in tasks],
        active_task_id=active.id if active else None,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, request: Request) -> TaskResponse:
    """Get a specific task by ID."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.store import TaskStore

    store = TaskStore(pool)
    task = await store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task)


@router.get("/tasks/{task_id}/artifacts")
async def get_task_artifacts(task_id: UUID, request: Request) -> list[dict[str, Any]]:
    """Artifact metadata for a task (§9/§33). The client sees id + filename + size + content-type + a
    download URL — NEVER the host path (that would leak the filesystem layout, §43)."""
    from pathlib import Path

    from sali.api.files import guess_content_type
    from sali.tasks.store import TaskStore

    pool = await _kernel(request).pool()
    out: list[dict[str, Any]] = []
    for a in await TaskStore(pool).artifacts(task_id):
        p = Path(a["artifact_path"])
        try:
            size = p.stat().st_size if p.is_file() else None
        except OSError:
            size = None
        out.append({
            "id": str(a["id"]),
            "filename": p.name,
            "artifact_type": a["artifact_type"],
            "tool_name": a["tool_name"],
            "size": size,
            "available": size is not None,
            "content_type": guess_content_type(p),
            "created_at": a["created_at"].isoformat() if a["created_at"] else None,
            "download_url": f"/api/v1/tasks/{task_id}/artifacts/{a['id']}/download",
        })
    return out


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/download")
async def download_artifact(task_id: UUID, artifact_id: UUID, request: Request) -> Any:
    """Stream one artifact, authorized (§9/§33). The file MUST resolve inside the task's authoritative
    workspace — a recorded path that escapes it (stale row, symlink, tampering) is refused, never served."""
    from pathlib import Path

    from starlette.responses import FileResponse

    from sali.api.files import guess_content_type, is_within
    from sali.tasks.store import TaskStore

    pool = await _kernel(request).pool()
    task = await TaskStore(pool).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    async with pool.acquire() as conn:
        path_str = await conn.fetchval(
            "SELECT artifact_path FROM task_artifact WHERE id = $1 AND task_id = $2",
            artifact_id, task_id)
    if path_str is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    root = Path(task.workspace_root) if task.workspace_root else None
    path = Path(path_str)
    if root is None or not is_within(path, root):
        # The API never serves a path outside the task's workspace — workspace authority stays intact.
        raise HTTPException(status_code=403, detail="artifact is outside the task workspace")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="artifact no longer available")
    return FileResponse(str(path), filename=path.name, media_type=guess_content_type(path))


@router.post("/tasks/{task_id}/files")
async def upload_task_file(
    task_id: UUID, request: Request, filename: str = "", _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Upload a file INTO a task's workspace (§9/§33) — "here is a file, work with this". The body is the
    raw file bytes; the name comes from ?filename= (or the X-Filename header). Written under
    <workspace>/uploads/ with a SANITIZED filename; a traversal attempt or oversize body is rejected and the
    workspace boundary is enforced. Recorded as an artifact so Sali (and later downloads) can find it.

    Raw-body (not multipart) upload keeps large transfers off the chat WebSocket (§33) and avoids pulling in
    a multipart parser — one file per request is all a mobile client needs."""
    from pathlib import Path

    from sali.api.files import MAX_UPLOAD_BYTES, safe_filename
    from sali.tasks.store import TaskStore

    pool = await _kernel(request).pool()
    task = await TaskStore(pool).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not task.workspace_root:
        raise HTTPException(status_code=409, detail="task has no bound workspace")

    name = safe_filename(filename or request.headers.get("x-filename", ""))
    if name is None:
        raise HTTPException(status_code=400, detail="a valid ?filename= (or X-Filename header) is required")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    dest_dir = (Path(task.workspace_root) / "uploads").resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    dest.write_bytes(data)
    await TaskStore(pool).record_artifact(task_id, str(dest), "created", tool_name="upload")
    return {"task_id": str(task_id), "filename": name, "size": len(data), "status": "stored"}


# ── Task control (§14/§34) — real backend authority, never a UI-only button ─────────────────────────

@router.post("/tasks/{task_id}/pause")
async def pause_task(
    task_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Pause a task safely (§14) — a legitimate suspend, not a failure; resumable as the same task."""
    from sali.tasks.store import TaskStore

    pool = await _kernel(request).pool()
    t = await TaskStore(pool, _publisher(request)).suspend(task_id, reason="paused from app")
    if t is None:
        raise HTTPException(status_code=404, detail="task not found or not pausable")
    return {"task_id": str(task_id), "status": t.status}


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Resume a paused task as the SAME task (§14)."""
    from sali.tasks.store import TaskStore

    pool = await _kernel(request).pool()
    t = await TaskStore(pool, _publisher(request)).resume(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found or not resumable")
    return {"task_id": str(task_id), "status": t.status}


@router.post("/tasks/{task_id}/abandon")
async def abandon_task(
    task_id: UUID, request: Request, ident: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Abandon a task = intent revocation (Prompt 12 §4). Tombstoned + all dependent work cancelled +
    the live row removed → no background path resurrects it. Distinct from pause: this will NOT resume."""
    from sali.runtime.revocation import revoke_intent

    pool = await _kernel(request).pool()
    result = await revoke_intent(pool, task_id, reason="user_revoked",
                                 by=ident.name, publisher=_publisher(request))
    return {"task_id": str(task_id), "status": "abandoned", **result}


@router.post("/tasks/{task_id}/revive")
async def revive_task(
    task_id: UUID, request: Request, ident: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Intentionally revive an abandoned task (Prompt 12 §25/§15). Creates a NEW task from the tombstoned
    objective and records the supersession — the old intent stays tombstoned, the new one is authoritative."""
    from sali.runtime.revocation import revive_intent
    from sali.tasks.revocation import RevocationStore
    from sali.tasks.store import TaskStore

    pool = await _kernel(request).pool()
    pub = _publisher(request)
    tomb = await RevocationStore(pool).get(task_id)
    if tomb is None:
        raise HTTPException(status_code=404, detail="no revoked intent for this task")
    objective = tomb.get("objective") or "revived task"
    store = TaskStore(pool, pub)
    new_task = await store.create(str(objective), [])
    await store.activate(new_task.id)
    await revive_intent(pool, task_id, new_task_id=new_task.id, publisher=pub)
    return {"revoked_task_id": str(task_id), "new_task_id": str(new_task.id), "status": "revived"}


# ── System health (§18) — resource stewardship surfaced with meaning ────────────────────────────────

@router.get("/system")
async def get_system(request: Request) -> dict[str, Any]:
    """One consolidated operational view for the System screen (§18): live host resources + state ladder +
    preservation flag (Prompt 12), subsystem health, model, and live WebSocket connection count. Metrics
    that can't be read degrade gracefully to null — never a fabricated number (golden rule)."""
    from sali.api.ws import manager as ws_manager
    from sali.runtime.resources import ResourceMonitor

    kernel = _kernel(request)
    resources: dict[str, Any]
    try:
        resources = _resource_view(await ResourceMonitor().snapshot())
    except Exception as exc:  # noqa: BLE001 - hardware read is best-effort; never crash the health view
        resources = {"available": False, "error": str(exc)[:200]}

    health: dict[str, Any] = {}
    try:
        from sali.runtime.health import HealthService
        pool = await kernel.pool()
        h = await (HealthService(pool, kernel.provider)).check()
        health = {"all_ok": h.all_ok, "datastore": h.datastore, "model": h.model,
                  "embedder": h.embedder, "internet": h.internet}
    except Exception as exc:  # noqa: BLE001
        health = {"all_ok": False, "error": str(exc)[:200]}

    return {
        "model": kernel.settings.model.chat_model,
        "resources": resources,
        "health": health,
        "websocket_connections": ws_manager.active_count,
    }


@router.get("/tasks/{task_id}/workspace")
async def get_task_workspace(task_id: UUID, request: Request) -> dict[str, Any]:
    """The task's authoritative workspace (Prompt 5 §27) — durable, from task state."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.store import TaskStore

    task = await TaskStore(pool).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": str(task_id), "workspace_root": task.workspace_root,
            "allowed_write_roots": task.allowed_write_roots, "workspace_mode": task.workspace_mode}


@router.get("/tasks/{task_id}/skills")
async def get_task_skills(task_id: UUID, request: Request) -> dict[str, Any]:
    """The skills selected (as durable snapshots) for a task (Prompt 5 §27). Content omitted for size."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.skills.store import SkillStore

    rows = await SkillStore(pool).for_task(task_id)
    return {"task_id": str(task_id), "skills": [
        {"name": r["name"], "path": r["path"], "content_hash": r["content_hash"],
         "score": r["score"]} for r in rows]}


@router.get("/tasks/{task_id}/research")
async def get_task_research(task_id: UUID, request: Request) -> dict[str, Any]:
    """A task's web-research findings + learning candidates (Prompt 5 §27)."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.research import ResearchStore

    rs = ResearchStore(pool)
    return {"task_id": str(task_id), "research": await rs.list_research(task_id),
            "candidates": await rs.list_candidates(task_id=task_id)}


@router.get("/cognitive")
async def get_cognitive_state(request: Request) -> dict[str, Any]:
    """The unified CognitiveState snapshot (Cognitive OS §46) — the single view the iPhone controller
    reconstructs 'what is Sali doing now?' from. Operational state only, never chain-of-thought."""
    runtime = _runtime(request)
    snap: dict[str, Any] = await runtime.snapshot()
    return snap


# ── Adaptive learning & behavior (Prompt 7 §41) ─────────────────────────────────────────────────────

@router.get("/learning")
async def get_learning(request: Request) -> dict[str, Any]:
    """Operational learning health for the iPhone controller (§28): candidate counts by state, open
    contradictions, and behavior-proposal counts — derived from durable state, bounded."""
    pool = await _kernel(request).pool()
    from sali.learning.behavior import BehaviorStore
    from sali.learning.candidates import LearningCandidateStore

    return {"candidates": await LearningCandidateStore(pool).counts(),
            "behavior": await BehaviorStore(pool).counts()}


@router.get("/learning/candidates")
async def get_learning_candidates(request: Request, scope: str | None = None) -> dict[str, Any]:
    """Active (verified/promoted/supported) learning candidates — the reusable knowledge, best first."""
    pool = await _kernel(request).pool()
    from sali.learning.candidates import LearningCandidateStore

    return {"candidates": await LearningCandidateStore(pool).active(scope=scope, limit=50)}


@router.get("/learning/recent")
async def get_learning_recent(request: Request) -> dict[str, Any]:
    """The most recently observed/updated learning candidates."""
    pool = await _kernel(request).pool()
    from sali.learning.candidates import LearningCandidateStore

    return {"recent": await LearningCandidateStore(pool).recent(limit=25)}


@router.get("/learning/contradictions")
async def get_learning_contradictions(request: Request, status: str | None = None) -> dict[str, Any]:
    """Recorded knowledge contradictions (§11) — never silently resolved; here for review."""
    pool = await _kernel(request).pool()
    from sali.learning.candidates import LearningCandidateStore

    return {"contradictions": await LearningCandidateStore(pool).contradictions(status=status)}


@router.get("/behavior/proposals")
async def get_behavior_proposals(request: Request) -> dict[str, Any]:
    """Pending behavior proposals the user can approve/reject (§28/§42)."""
    pool = await _kernel(request).pool()
    from sali.learning.behavior import BehaviorStore

    return {"proposals": await BehaviorStore(pool).pending()}


@router.get("/behavior/profile")
async def get_behavior_profile(request: Request) -> dict[str, Any]:
    """The DERIVED behavior profile (§19) — reconstructed from durable records, never a stored blob."""
    pool = await _kernel(request).pool()
    from sali.learning.behavior import BehaviorStore

    return (await BehaviorStore(pool).profile()).to_dict()


@router.post("/behavior/proposals/{proposal_id}/approve")
async def approve_behavior_proposal(
    proposal_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """The user is the final authority on behavior that changes how Sali works (§42): accept a proposal
    so it affects future planning. Scope is preserved — this never makes a preference global."""
    pool = await _kernel(request).pool()
    from sali.learning.behavior import BehaviorStore

    ok = await BehaviorStore(pool, _publisher(request)).accept(proposal_id, by="user")
    if not ok:
        raise HTTPException(status_code=404, detail="proposal not found or not pending")
    return {"proposal_id": str(proposal_id), "status": "accepted"}


@router.post("/behavior/proposals/{proposal_id}/reject")
async def reject_behavior_proposal(
    proposal_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Reject a behavior proposal — it will not affect future behavior (§16)."""
    pool = await _kernel(request).pool()
    from sali.learning.behavior import BehaviorStore

    ok = await BehaviorStore(pool, _publisher(request)).reject(proposal_id, by="user")
    if not ok:
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"proposal_id": str(proposal_id), "status": "rejected"}


# ── Lifetime memory & experience (Prompt: lifetime memory §40/§41) ──────────────────────────────────

@router.get("/memory")
async def get_memory(request: Request) -> dict[str, Any]:
    """Operational memory state for the iPhone controller (§40): counts by layer, experience count,
    open conflicts, and the most recent experiences. Never chain-of-thought — durable state only."""
    pool = await _kernel(request).pool()
    from sali.learning.experience import ExperienceStore

    store = ExperienceStore(pool)
    return {"counts": await store.counts(), "recent_experiences": await store.recent(limit=10)}


@router.get("/experiences")
async def get_experiences(request: Request, objective: str | None = None) -> dict[str, Any]:
    """What experiences shaped Sali. With ?objective=…, the experiences relevant to that objective
    (the same bounded retrieval planning uses); otherwise the most recent."""
    pool = await _kernel(request).pool()
    from sali.learning.experience import ExperienceStore

    store = ExperienceStore(pool)
    if objective:
        return {"experiences": await store.relevant_experiences(objective=objective, limit=10)}
    return {"experiences": await store.recent(limit=25)}


@router.get("/memory/conflicts")
async def get_memory_conflicts(request: Request) -> dict[str, Any]:
    """Recorded memory contradictions (§14/§47) — conflicting knowledge, never silently overwritten."""
    pool = await _kernel(request).pool()
    from sali.learning.experience import ExperienceStore

    return {"conflicts": await ExperienceStore(pool).conflicts()}


@router.get("/memory/{memory_id}")
async def get_memory_provenance(memory_id: UUID, request: Request) -> dict[str, Any]:
    """Why Sali believes a memory (§11): its source, the task/run it came from, and its evidence."""
    pool = await _kernel(request).pool()
    from sali.learning.experience import ExperienceStore

    prov = await ExperienceStore(pool).provenance(memory_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return prov


# ── Persistent agency: activities, capabilities, side effects, cleanup (§58/§59) ────────────────────

@router.get("/activities")
async def get_activities(request: Request) -> dict[str, Any]:
    """The activities still open (completion-discipline worklist) + counts (§6)."""
    pool = await _kernel(request).pool()
    from sali.tasks.activity import ActivityStore

    store = ActivityStore(pool)
    return {"open": await store.open_activities(), "counts": await store.counts()}


@router.get("/capabilities")
async def get_capabilities(request: Request, status: str | None = None) -> dict[str, Any]:
    """What Sali has learned to do — evidence-backed, environment-scoped capabilities (§12-16)."""
    pool = await _kernel(request).pool()
    from sali.learning.capability import CapabilityStore

    store = CapabilityStore(pool)
    return {"capabilities": await store.list(status=status), "counts": await store.counts()}


@router.get("/capabilities/overview")
async def get_capability_overview(request: Request) -> dict[str, Any]:
    """Self-describing capability view (§34): built-in tool groups + learned capabilities, no fakery."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.runtime import cognitive
    from sali.tools.registry import default_registry

    return await cognitive.capability_overview(pool, default_registry())


@router.get("/capability-acquisitions")
async def get_capability_acquisitions(request: Request) -> dict[str, Any]:
    """Capability-acquisition attempts in progress — the gaps Sali is working to close (§5/§10)."""
    pool = await _kernel(request).pool()
    from sali.learning.capability_acquisition import CapabilityAcquisitionStore

    store = CapabilityAcquisitionStore(pool)
    return {"in_progress": await store.open(), "counts": await store.counts()}


@router.get("/external-entities")
async def get_external_entities(request: Request) -> dict[str, Any]:
    """External identities/accounts and their lifecycle state — no secrets, ever (§11/§12)."""
    pool = await _kernel(request).pool()
    from sali.tasks.external import ExternalEntityStore

    store = ExternalEntityStore(pool)
    return {"unfinished": await store.unfinished(), "counts": await store.counts()}


# ── Digital life: objects, actions, obligations, commitments (Prompt 9 §51) ─────────────────────────

@router.get("/digital-objects")
async def get_digital_objects(request: Request, object_type: str | None = None) -> dict[str, Any]:
    """DigitalLifeObjects Sali owns/maintains — accounts, sites, repos, … with lifecycle state (§2)."""
    pool = await _kernel(request).pool()
    from sali.tasks.external import ExternalEntityStore

    return {"objects": await ExternalEntityStore(pool).list_objects(object_type=object_type)}


@router.get("/digital-actions")
async def get_digital_actions(request: Request) -> dict[str, Any]:
    """Digital actions still open — service interactions not yet verified/maintained (§7)."""
    pool = await _kernel(request).pool()
    from sali.tasks.digital import DigitalActionStore

    store = DigitalActionStore(pool)
    return {"unfinished": await store.unfinished(), "counts": await store.counts()}


@router.get("/obligations")
async def get_obligations(request: Request, due_only: bool = False) -> dict[str, Any]:
    """Open obligations — Sali's unfinished business, never silently abandoned (§9/§27)."""
    pool = await _kernel(request).pool()
    from sali.tasks.commitments import ObligationStore

    store = ObligationStore(pool)
    return {"open": await store.open(due_only=due_only), "counts": await store.counts()}


@router.get("/commitments")
async def get_commitments(request: Request) -> dict[str, Any]:
    """Open commitments — enduring responsibilities distinct from tasks (§25/§26)."""
    pool = await _kernel(request).pool()
    from sali.tasks.commitments import CommitmentStore

    store = CommitmentStore(pool)
    return {"open": await store.open(), "counts": await store.counts()}


# ── Autonomous life: goals, initiatives, routines, relationships, timeline (Prompt 10 §70) ──────────

@router.get("/current-life")
async def get_current_life(request: Request) -> dict[str, Any]:
    """A compact operational snapshot of Sali's ongoing life (§78) — awake?, current activity, active
    goal/commitment, open initiatives, what he's waiting for, next wake. Never chain-of-thought."""
    snap: dict[str, Any] = await _runtime(request).snapshot()
    return snap


@router.get("/goals")
async def get_goals(request: Request) -> dict[str, Any]:
    """Sali's active durable goals — objectives that outlive a single task (§5/§6)."""
    pool = await _kernel(request).pool()
    from sali.tasks.goals import GoalStore

    store = GoalStore(pool)
    return {"active": await store.active(), "counts": await store.counts()}


@router.get("/initiatives")
async def get_initiatives(request: Request) -> dict[str, Any]:
    """Open initiatives — opportunities Sali has noticed, deterministically scored (§9/§10)."""
    pool = await _kernel(request).pool()
    from sali.tasks.initiatives import InitiativeStore

    store = InitiativeStore(pool)
    return {"open": await store.open(), "counts": await store.counts()}


@router.get("/routines")
async def get_routines(request: Request) -> dict[str, Any]:
    """Recurring activities Sali maintains, with their next fire time + failure history (§8)."""
    pool = await _kernel(request).pool()
    from sali.tasks.routines import RoutineStore

    store = RoutineStore(pool)
    return {"routines": await store.list(), "counts": await store.counts()}


@router.get("/relationships")
async def get_relationships(request: Request) -> dict[str, Any]:
    """People Sali knows and how — evidence-grounded relationship shells (§23/§24)."""
    pool = await _kernel(request).pool()
    from sali.tasks.people import PersonStore

    store = PersonStore(pool)
    return {"people": await store.list(), "counts": await store.counts()}


@router.get("/timeline")
async def get_timeline(request: Request, limit: int = 40) -> dict[str, Any]:
    """A structured history of meaningful life events (§29/§60) — derived from the durable event log,
    not a chat transcript. Filtered to goals/commitments/capabilities/actions/experiences/life."""
    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, created_at, payload FROM event "
            "WHERE event_type LIKE 'goal.%' OR event_type LIKE 'commitment.%' "
            "  OR event_type LIKE 'capability.%' OR event_type LIKE 'digital_action.%' "
            "  OR event_type LIKE 'experience.%' OR event_type LIKE 'life.%' "
            "  OR event_type LIKE 'digital_object.%' OR event_type='obligation.created' "
            "ORDER BY seq DESC LIMIT $1", min(limit, 200))
    return {"timeline": [{"type": r["event_type"],
                          "at": r["created_at"].isoformat() if r["created_at"] else None,
                          "detail": {k: v for k, v in (r["payload"] or {}).items()
                                     if k not in ("event_id", "session_id")}} for r in rows]}


# ── Human interaction & natural consent (Prompt 11 §68) ─────────────────────────────────────────────

@router.get("/consent")
async def get_consent(request: Request) -> dict[str, Any]:
    """Pending natural-consent requests — what Sali is waiting on the user to authorize (§57/§68).
    Structured state behind the natural-language ask; never a y/n gate."""
    pool = await _kernel(request).pool()
    from sali.tasks.consent import ConsentStore

    store = ConsentStore(pool)
    return {"pending": await store.open(), "counts": await store.counts()}


@router.post("/consent/{consent_id}/respond")
async def respond_consent(
    consent_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Resolve a consent request from a natural-language reply (§4) — the reply is the consent signal.
    The body is {"response": "yes, do it" | "only the first two" | "not now" | "leave it" | …}."""
    pool = await _kernel(request).pool()
    from sali.tasks.consent import ConsentStore

    body = await request.json()
    outcome = await ConsentStore(pool, _publisher(request)).resolve(consent_id, str(body.get("response", "")))
    return {"consent_id": str(consent_id), "status": outcome.status, "note": outcome.note}


# ── Async life: conversation threads, pending questions, behavioral context (§33/§37) ────────────────

@router.get("/conversations")
async def get_conversations(request: Request, person: str | None = None) -> dict[str, Any]:
    """Open conversation threads — Sali keeps several going; waiting on one never freezes the rest (§7)."""
    pool = await _kernel(request).pool()
    from sali.tasks.conversation import ConversationThreadStore

    store = ConversationThreadStore(pool)
    return {"threads": await store.open_threads(person_name=person), "counts": await store.counts()}


@router.get("/pending-questions")
async def get_pending_questions(request: Request, person: str | None = None) -> dict[str, Any]:
    """Questions Sali is waiting on — durable, dependency-aware; a late answer routes back here (§4/§24)."""
    pool = await _kernel(request).pool()
    from sali.tasks.conversation import PendingQuestionStore

    store = PendingQuestionStore(pool)
    return {"waiting": await store.waiting(person_name=person), "counts": await store.counts()}


@router.post("/pending-questions/answer")
async def answer_pending_question(
    request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Route a late free-text answer to the right pending question (§3/§6/§23). Body:
    {"person": "Almir", "answer": "go with option B", "thread_id"?: "…"}. Ambiguity → clarification list."""
    pool = await _kernel(request).pool()
    from sali.tasks.conversation import PendingQuestionStore

    body = await request.json()
    store = PendingQuestionStore(pool, _publisher(request))
    thread = body.get("thread_id")
    routing = await store.route_answer(
        str(body.get("answer", "")), person_name=body.get("person"),
        thread_id=UUID(thread) if thread else None)
    if routing.matched is not None:
        answered = await store.answer(UUID(str(routing.matched["id"])), str(body.get("answer", "")))
        return {"status": "answered" if answered else "not_waiting",
                "question_id": str(routing.matched["id"])}
    if routing.ambiguous:
        return {"status": "ambiguous",
                "candidates": [{"id": str(q["id"]), "question": q["question"]} for q in routing.ambiguous]}
    return {"status": "no_pending_question"}


@router.get("/behavioral-context")
async def get_behavioral_context(request: Request, person: str | None = None) -> dict[str, Any]:
    """The bounded, evidence-backed behavioral context for interacting now (§19/§33) — preferences +
    learned tendencies + relationship, derived from durable state. Never hidden chain-of-thought."""
    pool = await _kernel(request).pool()
    from sali.runtime.behavioral_context import assemble_behavioral_context

    return await assemble_behavioral_context(pool, person_name=person)


# ── Self-preservation & intent revocation (Prompt 12 §27) ───────────────────────────────────────────

def _resource_view(snap: dict[str, Any]) -> dict[str, Any]:
    """Flatten the runtime ResourceMonitor snapshot ({state, reading:{…fracs}, preserve}) into the flat,
    percentage-based shape the iPhone app consumes (docs/API_REFERENCE §13). Fractions (0..1) become
    percentages (0..100); an unreadable metric stays null — never a fabricated number (golden rule)."""
    reading = snap.get("reading") or {}

    def pct(x: Any) -> float | None:
        return round(float(x) * 100, 1) if isinstance(x, (int, float)) else None

    return {
        "state": snap.get("state"),
        "preservation": bool(snap.get("preserve")),
        "gpu_util_pct": pct(reading.get("gpu_util_frac")),
        "vram_used_pct": pct(reading.get("vram_used_frac")),
        "ram_used_pct": pct(reading.get("ram_used_frac")),
        "disk_used_pct": pct(reading.get("disk_used_frac")),
        "cpu_load": reading.get("cpu_load_per_core"),
        "temperature_c": reading.get("gpu_temp_c"),
        "available": bool(reading.get("ok")),
        "raw": snap,  # nested source, for the Activity "technical details" view
    }


@router.get("/resources")
async def get_resources(request: Request) -> dict[str, Any]:
    """Live host resource state — VRAM/GPU/RAM/CPU/disk + the safe→emergency state + whether Sali is in
    preservation mode (§27). Observed deterministically, never guessed."""
    from sali.runtime.resources import ResourceMonitor

    return _resource_view(await ResourceMonitor().snapshot())


@router.get("/resource-incidents")
async def get_resource_incidents(request: Request, kind: str | None = None) -> dict[str, Any]:
    """Durable host-endangering incidents — negative operational knowledge that shapes future plans (§21)."""
    pool = await _kernel(request).pool()
    from sali.tasks.incidents import ResourceIncidentStore

    store = ResourceIncidentStore(pool)
    return {"incidents": await store.relevant(kind=kind, limit=25), "counts": await store.counts()}


@router.get("/intent/revoked")
async def get_revoked_intents(request: Request) -> dict[str, Any]:
    """Intentions the user abandoned — tombstoned so no background path resurrects them (§4/§24)."""
    pool = await _kernel(request).pool()
    from sali.tasks.revocation import RevocationStore

    store = RevocationStore(pool)
    return {"revoked": await store.recent(), "counts": await store.counts()}


@router.get("/side-effects")
async def get_side_effects(request: Request) -> dict[str, Any]:
    """Consequential actions still open (unfinished side effects) + counts (§7/§8)."""
    pool = await _kernel(request).pool()
    from sali.tasks.side_effects import SideEffectStore

    store = SideEffectStore(pool)
    return {"unfinished": await store.unfinished(), "counts": await store.counts()}


@router.get("/cleanup")
async def get_cleanup(request: Request) -> dict[str, Any]:
    """Workspace-cleanup ledger — pending/failed cleanups Sali can resume (§26)."""
    pool = await _kernel(request).pool()
    from sali.tasks.cleanup import WorkspaceCleanupStore

    store = WorkspaceCleanupStore(pool)
    return {"pending": await store.pending(), "counts": await store.counts()}


@router.get("/tasks/{task_id}/question")
async def get_task_question(task_id: UUID, request: Request) -> dict[str, Any]:
    """The pending user-clarification question for a task, if any (Cognitive OS §44)."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.coordination import QuestionStore

    return {"task_id": str(task_id), "pending": await QuestionStore(pool).pending(task_id)}


@router.get("/tasks/{task_id}/decisions")
async def get_task_decisions(task_id: UUID, request: Request) -> dict[str, Any]:
    """The active decision ledger for a task (Prompt 6 §30)."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.ledger import DecisionStore

    return {"task_id": str(task_id), "decisions": await DecisionStore(pool).active(task_id)}


@router.get("/tasks/{task_id}/phases")
async def get_task_phases(task_id: UUID, request: Request) -> dict[str, Any]:
    """A task's phases + summaries (Prompt 6 §31)."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.ledger import PhaseStore

    return {"task_id": str(task_id), "phases": await PhaseStore(pool).phases(task_id)}


@router.get("/tasks/{task_id}/context")
async def get_task_context(task_id: UUID, request: Request) -> dict[str, Any]:
    """Context-efficiency metadata for a task (Prompt 6 §46): the operational budget, the live capsule's
    token estimate + source manifest, and the latest durable context checkpoint. Operational only —
    never any prompt/tool content or chain-of-thought."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.runtime import context_budget
    from sali.runtime.capsule import build_capsule, render_capsule
    from sali.tasks.store import TaskStore

    task = await TaskStore(pool).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    cap = await build_capsule(pool, task)
    rendered = render_capsule(cap)
    async with pool.acquire() as conn:
        cp = await conn.fetchrow(
            "SELECT id, run_id, step_seq, context_version, source_ids, token_estimate, reason, created_at "
            "FROM context_checkpoint WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", task_id)
    return {
        "task_id": str(task_id),
        "context_limit": context_budget.resolve_limit(kernel.provider, kernel.settings),
        "output_reserve": context_budget.output_reserve(kernel.settings),
        "capsule_token_estimate": kernel.provider.count_tokens(rendered),
        "capsule_source_ids": cap.source_ids(),
        "next_action": cap.next_action,
        "latest_checkpoint": (dict(cp) if cp else None),
    }


@router.get("/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """All human-editable skills Sali can discover (Prompt 5 §27)."""
    from pathlib import Path

    from sali.skills.discovery import discover_skills

    kernel = _kernel(request)
    root = kernel.settings.permissions.skills_root
    return [{"name": s.name, "title": s.title, "tags": s.tags, "path": s.path}
            for s in discover_skills(Path(root))]


@router.get("/tasks/{task_id}/reviews")
async def get_task_reviews(task_id: UUID, request: Request) -> dict[str, Any]:
    """Durable reviewer state for a task (Prompt 4 §14): every review attempt, and the latest verdict —
    so a client can show 'N/M requirements verified', the current reviewer status (PASS / NEEDS_REWORK /
    BLOCKED), the latest failures, and the attempt count. Read-only."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.tasks.reviewer import TaskReviewer

    reviewer = TaskReviewer(pool)
    reviews = await reviewer.reviews(task_id)
    latest = reviews[-1] if reviews else None
    return {
        "task_id": str(task_id),
        "attempts": len(reviews),
        "reviewer_status": latest["status"] if latest else None,
        "latest": latest,
        "reviews": reviews,
    }


# ── Schedules ─────────────────────────────────────────────────────────────────

@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules(request: Request) -> list[ScheduleResponse]:
    """List all schedules."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.scheduler.store import ScheduleStore

    store = ScheduleStore(pool)
    schedules = await store.list_all()
    return [
        ScheduleResponse(
            id=s.id, name=s.name, kind=s.kind, spec=s.spec, prompt=s.prompt,
            enabled=s.enabled, next_run_at=s.next_run_at, last_run_at=s.last_run_at,
            last_status=s.last_status,
        )
        for s in schedules
    ]


# ── Event Recovery ────────────────────────────────────────────────────────────

@router.get("/events")
async def get_events(
    request: Request,
    after_seq: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Durable events for reconnect recovery (§29). Ordered by the monotonic `seq` (the event table's
    GENERATED-IDENTITY primary key) — NOT the random UUID `id`. A client tracks the highest `seq` it has
    seen from the live stream and, after a disconnect, requests everything after it to close the gap."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    limit = max(1, min(limit, 500))

    async with pool.acquire() as conn:
        if after_seq is not None:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                "FROM event WHERE seq > $1 ORDER BY seq LIMIT $2",
                after_seq, limit)
        else:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                "FROM event ORDER BY seq DESC LIMIT $1", limit)
            rows = list(reversed(rows))

    return [
        {
            "seq": r["seq"],
            "type": r["event_type"],
            "subject_type": r["subject_type"],
            "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
            "payload": dict(r["payload"]) if r["payload"] else {},
            "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _task_to_response(task: Any) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        objective=task.objective,
        status=task.status,
        result=task.result,
        is_primary=task.is_primary,
        workspace_root=task.workspace_root,
        workspace_mode=task.workspace_mode,
        steps=[
            TaskStepResponse(
                seq=s.seq, description=s.description, status=s.status,
                note=s.note, attempts=s.attempts, last_error=s.last_error,
                failure_class=s.failure_class, verified=s.verified,
                checkpoint=s.checkpoint,
            )
            for s in task.steps
        ],
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_heartbeat=task.last_heartbeat,
        interrupted_at=task.interrupted_at,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        superseded_by=task.superseded_by,
    )
