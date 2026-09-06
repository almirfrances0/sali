"""REST API routes — the HTTP contract for mobile clients.

All message submissions go through the AgentRuntimeCoordinator,
which serializes execution and prevents concurrent agent loops.
"""

from __future__ import annotations

from typing import Any
import contextlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from sali.api.auth import Identity, require_controller, require_identity
from fastapi import HTTPException
from sali.api.models import (
    ConversationMessage,
    ConversationState,
    HealthResponse,
    MessageAttachment,
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

    task_count = None
    try:
        async with pool.acquire() as conn:
            mem_count = await conn.fetchval(
                "SELECT count(*) FROM memory WHERE valid_until IS NULL")
            task_count = await conn.fetchval(
                "SELECT count(*) FROM task WHERE status IN "
                "('open','running','waiting','blocked','paused','waiting_for_user')")
    except Exception:
        mem_count = None

    return SystemStatus(
        version=__version__,
        model=kernel.settings.model.chat_model,
        session_id=str(persistent_session_id()),
        active_task_id=active.id if active else None,
        uptime_s=__import__("time").time() - _API_STARTED,
        memory_count=mem_count,
        task_count=task_count,
    )


# ── Conversation ──────────────────────────────────────────────────────────────

from sali.api.models import RememberRequest, ScheduleCreateRequest

_API_STARTED = __import__("time").time()


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
            "SELECT id, conversation_id, seq, role, content, model, created_at, attachment "
            "FROM message WHERE conversation_id = $1 ORDER BY seq DESC LIMIT $2",
            session_id, limit)

    import json as _json

    def _att(raw: Any) -> MessageAttachment | None:
        # A file Sali sent, carried ON its reply message (one durable message; renders as a download
        # card inline and survives a reload). asyncpg returns jsonb as a str by default.
        if not raw:
            return None
        d = raw if isinstance(raw, dict) else _json.loads(raw)
        if not d.get("filename") or not d.get("download_url"):
            return None
        return MessageAttachment(
            artifact_id=str(d.get("artifact_id") or ""), filename=d["filename"],
            size=d.get("size"), download_url=d["download_url"], kind=d.get("kind"))

    messages = [
        ConversationMessage(
            id=r["id"], session_id=r["conversation_id"], seq=r["seq"], role=r["role"],
            content=r["content"], model=r["model"], created_at=r["created_at"],
            attachment=_att(r["attachment"]),
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
    body: SendMessageRequest, request: Request,
    identity: Identity = Depends(require_controller),
) -> SendMessageResponse:
    """Send a message to Sali. Serialized through the runtime.

    If another turn is running (terminal or API), this message is queued.
    The response streams via WebSocket.
    """
    runtime = _runtime(request)

    if not body.content.strip() and not (body.image_ref or body.image_b64):
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if runtime is None:
        raise HTTPException(status_code=503, detail="Sali's runtime is not attached to this API")

    # Resolve an attached image to bytes (bounded to the conversation workspace) so the runtime can fold a
    # description into the turn. Bytes are never persisted — only the derived text (see handle_message).
    import base64
    import contextlib
    from pathlib import Path
    image_bytes: bytes | None = None
    if body.image_ref:
        works = Path(_kernel(request).settings.permissions.workspace).expanduser()
        root = (works / "conversations").resolve()
        candidate = Path(body.image_ref).resolve()
        if root in candidate.parents and candidate.is_file():
            with contextlib.suppress(Exception):
                image_bytes = candidate.read_bytes()
    elif body.image_b64:
        with contextlib.suppress(Exception):
            image_bytes = base64.b64decode(body.image_b64)

    import asyncio

    async def _submit() -> None:
        try:
            # Route through the attention authority (Prompt 2) — same semantics as terminal + iOS. The turn's
            # completion is signalled by the canonical, durable `agent.final` event the coordinator already
            # publishes (which replays on reconnect); we no longer emit a redundant, non-durable,
            # sequence-less `message.completed` broadcast that the recovery contract couldn't replay
            # (Final audit §29/§31).
            content = body.content
            if image_bytes is not None:
                # Vision fold in the background task (the 202 already returned). Reuse the local vision model
                # via the runtime's provider; describe_image shares the COGNITION lease with generation, so
                # the one cognition slot is preserved. Never let vision break the turn.
                import contextlib as _ctx
                with _ctx.suppress(Exception):
                    _desc = await runtime._loop.provider.describe_image(
                        "Describe this image faithfully. Focus on the SCENE and the MOMENT — where it "
                        "is, what is happening, the overall mood — and for an object or screen its make, "
                        "model, text and distinguishing marks. For people, note ONLY what the scene "
                        "shows (roughly who is present and what they're doing), NOT a catalogue of "
                        "anyone's features or looks. State only what you can actually SEE. Do not guess "
                        "at anyone's identity, name or relationship.",
                        image_bytes)
                    if _desc and _desc.strip():
                        # The description is Sali's OWN observation, and it must not be mistaken for
                        # ground truth about WHO someone is. Almir knows his own people; a vision model
                        # does not. Sali telling him a photo of his wife "isn't her" (which happened) is
                        # the exact hallucination he objects to — so the fold says plainly whose claim
                        # outranks whose, and asks him rather than contradicting him.
                        content = (content + "\n\n" if content.strip() else "") \
                            + (f"[You looked at an image Almir shared. What you can see: "
                               f"{_desc.strip()}\n"
                               f"That is your own observation of the scene only — it is NOT knowledge "
                               f"of who anyone is. If Almir tells you who or what this is, he is right "
                               f"and you remember it. Never contradict him about the identity of his "
                               f"own people or things; if you are unsure, ask. If this is personal — "
                               f"his family, his home, a moment he's sharing — meet it warmly and speak "
                               f"to HIM about it; don't appraise how his people look.]")
            await runtime.handle_message(content, origin="api")
        except Exception as exc:
            from sali.api.ws import manager as ws_manager
            await ws_manager.broadcast_event("error", {"error": str(exc)[:200]})
        finally:
            _IN_FLIGHT.discard(asyncio.current_task())

    # Record HOW Almir is connected for this turn — the channel (iPhone vs terminal) and local-vs-remote,
    # from the real transport — so Sali's self-state knows he's on his phone (a local path won't reach
    # him; send files with send_file), not at this machine (§17-28). Best-effort; never blocks the turn.
    with contextlib.suppress(Exception):
        from sali.net.connection import classify as _classify_conn
        _platform = None
        _did = getattr(identity, "device_id", None)
        if _did is not None:
            _pool = await _kernel(request).pool()
            async with _pool.acquire() as _c:
                _platform = await _c.fetchval("SELECT platform FROM sali.api_device WHERE id = $1", _did)
        runtime.note_connection(_classify_conn(identity, request, platform=_platform))

    # The reply streams over the WebSocket, so the HTTP request returns immediately — but the task
    # must be ANCHORED. asyncio keeps only a weak reference to a bare create_task(), so under GC
    # pressure a turn could vanish mid-thought with the client already told "accepted".
    task = asyncio.create_task(_submit())
    _IN_FLIGHT.add(task)

    # Deliberately no run_id: the runtime creates the real one after this returns (see the model).
    return SendMessageResponse(status="accepted")


# Strong references to in-flight message handlers (see send_message). Concurrency between them is
# arbitrated by the runtime's attention model and the one cognition slot, not here.
_IN_FLIGHT: set[Any] = set()


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

    responses = []
    for t in tasks:
        resp = _task_to_response(t)
        # Live activity only for what is actually moving — a settled task has nothing to say, and this
        # costs two small queries per running task rather than per row.
        if t.status == "running":
            with contextlib.suppress(Exception):
                live = await store.live_activity(t.id)
                resp.working = bool(live.get("working"))
                resp.current_step = live.get("current_step")
                resp.last_tool = live.get("last_tool")
                resp.last_tool_status = live.get("last_tool_status")
                resp.last_detail = live.get("last_detail")
                resp.last_activity_at = live.get("last_activity_at")
        responses.append(resp)

    return TaskListResponse(
        tasks=responses,
        active_task_id=active.id if active else None,
    )


@router.get("/tasks/history")
async def task_history(request: Request, limit: int = 30) -> list[dict[str, Any]]:
    """Finished/failed/cancelled tasks from the durable sali-works archive (audit: the DB rows are
    archived + deleted on completion, so finished work vanished from the app the moment it was done)."""
    import contextlib
    import json as _json
    from pathlib import Path

    base = Path.home() / "Desktop" / "sali-works" / "tasks"
    items: list[dict[str, Any]] = []
    if base.is_dir():
        for d in base.iterdir():
            f = d / "meta.json"
            if not f.is_file():
                continue
            with contextlib.suppress(Exception):
                m = _json.loads(f.read_text(encoding="utf-8"))
                if m.get("status") in ("done", "failed", "abandoned", "cancelled", "superseded"):
                    def _iso(v: Any) -> str | None:
                        """meta.json stores str(datetime) — "2026-09-02 22:00:34.833226+00:00", with a
                        SPACE. ISO8601DateFormatter (what the iOS client uses) requires the 'T', so every
                        history row failed to decode and the whole screen rendered empty. Normalise here,
                        at the contract boundary, rather than making each client tolerant."""
                        if not v:
                            return None
                        t = str(v).strip().replace(" ", "T", 1)
                        return t if t.endswith("Z") or "+" in t[10:] or "-" in t[10:] else t + "Z"

                    items.append({
                        "id": m.get("task_id"), "objective": m.get("objective", ""),
                        "status": m.get("status"), "result": m.get("result"),
                        "created_at": _iso(m.get("created_at")),
                        "updated_at": _iso(m.get("updated_at")),
                        "workspace_root": m.get("workspace_root"),
                    })
    items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return items[:limit]


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
    if not out:
        # Completed tasks are archived out of the DB — serve the durable archive copies instead
        # (audit: artifacts 404'd the moment a task finished).
        import contextlib
        import json as _json
        adir = Path.home() / "Desktop" / "sali-works" / "tasks" / str(task_id)
        f = adir / "artifacts.json"
        if f.is_file():
            with contextlib.suppress(Exception):
                for a in _json.loads(f.read_text(encoding="utf-8")):
                    aid = str(a.get("id") or "")
                    name = a.get("archived_name")
                    p = (adir / "artifacts" / name) if name else None
                    available = bool(p is not None and p.is_file())
                    if not aid:
                        continue
                    out.append({
                        "id": aid,
                        "filename": a.get("filename") or (name.split("_", 1)[1] if name and "_" in name
                                                          else name) or "artifact",
                        "artifact_type": a.get("artifact_type"),
                        "tool_name": a.get("tool_name"),
                        "size": (p.stat().st_size if available else a.get("size")),
                        "available": available,
                        "content_type": guess_content_type(p) if available else "application/octet-stream",
                        "created_at": a.get("created_at"),
                        "download_url": f"/api/v1/tasks/{task_id}/artifacts/{aid}/download",
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
        # Completed tasks live in the archive — serve the durable copy, confined to the archive dir.
        base = (Path.home() / "Desktop" / "sali-works" / "tasks" / str(task_id) / "artifacts").resolve()
        if base.is_dir():
            for f in sorted(base.glob(f"{artifact_id}_*")):
                p = f.resolve()
                if is_within(p, base) and p.is_file():
                    nice = p.name.split("_", 1)[1] if "_" in p.name else p.name
                    return FileResponse(str(p), filename=nice, media_type=guess_content_type(p))
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


@router.get("/research")
async def list_research_reports(request: Request) -> list[dict[str, Any]]:
    """Recent downloadable research reports (Almir §): the SUMMARIES live in the chat; the FULL write-ups
    are here to download ('summarise then for more i download the file')."""
    from sali.runtime.reports import ResearchReportStore

    pool = await _kernel(request).pool()
    return await ResearchReportStore(pool).list(limit=50)


@router.get("/research/{report_id}/download")
async def download_research_report(report_id: UUID, request: Request) -> Any:
    """Stream one research report's markdown, confined to the sali-works/research directory (a stored
    path that escapes it — stale row, tampering — is refused, never served)."""
    from pathlib import Path

    from starlette.responses import FileResponse

    from sali.api.files import is_within
    from sali.runtime.reports import ResearchReportStore

    pool = await _kernel(request).pool()
    report = await ResearchReportStore(pool).get(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    path = Path(report["path"])
    base = (Path.home() / "Desktop" / "sali-works" / "research").resolve()
    if not is_within(path, base):
        raise HTTPException(status_code=403, detail="report is outside the research directory")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="report file no longer available")
    return FileResponse(str(path), filename=path.name, media_type="text/markdown; charset=utf-8")


@router.get("/files/sent/{file_id}/download")
async def download_sent_file(file_id: UUID, request: Request) -> Any:
    """Stream a file Sali SENT to Almir (send_file tool), confined to the sali-works/sends directory."""
    from pathlib import Path

    from starlette.responses import FileResponse

    from sali.api.files import guess_content_type, is_within
    from sali.runtime.sent_files import SentFileStore

    pool = await _kernel(request).pool()
    rec = await SentFileStore(pool).get(file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="file not found")
    path = Path(rec["path"])
    base = (Path.home() / "Desktop" / "sali-works" / "sends").resolve()
    if not is_within(path, base):
        raise HTTPException(status_code=403, detail="file is outside the sends directory")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="file no longer available")
    return FileResponse(str(path), filename=rec["filename"], media_type=guess_content_type(path))


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
    await TaskStore(pool, _publisher(request)).record_artifact(
        task_id, str(dest), "created", tool_name="upload")
    return {"task_id": str(task_id), "filename": name, "size": len(data), "status": "stored"}


@router.post("/files")
async def upload_chat_file(
    request: Request, filename: str = "", _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Attach a file to the ONGOING conversation with NO task (§9/§33) — "here, look at this" in plain chat.
    Raw body; name from ?filename= (or the X-Filename header). Written under
    <workspace>/conversations/<session_id>/uploads/ with a SANITIZED name; traversal + oversize rejected.
    Emits a `desktop.observed` perception so Sali passively becomes aware of the file next turn — no new
    tool, no new cognition. Returns {filename, size, ref}; `ref` is what conversation/message's image_ref
    references for a vision fold."""
    import contextlib
    from pathlib import Path

    from sali.api.files import MAX_UPLOAD_BYTES, safe_filename
    from sali.runtime.session import persistent_session_id

    name = safe_filename(filename or request.headers.get("x-filename", ""))
    if name is None:
        raise HTTPException(status_code=400, detail="a valid ?filename= (or X-Filename header) is required")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    session_id = persistent_session_id()
    works = Path(_kernel(request).settings.permissions.workspace).expanduser()
    dest_dir = (works / "conversations" / str(session_id) / "uploads").resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    dest.write_bytes(data)

    pub = _publisher(request)
    if pub is not None:
        with contextlib.suppress(Exception):
            await pub.emit(
                "desktop.observed", origin="user", session_id=session_id,
                data={"kind": "file_uploaded",
                      "summary": f"Almir attached {name} to the conversation",
                      "path": str(dest)})

    return {"filename": name, "size": len(data), "ref": str(dest)}


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


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: UUID, request: Request, ident: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Cancel a task via the same intent-revocation path /abandon uses (Turn 2). Was: bare
    store.cancel() which left the row 'cancelled' and revive-able through /resume, with no
    tombstone written to `revoked_intent` and no dependent-work propagation. Now: tombstone +
    propagation + archive - a cancelled task is dead. The mobile app posts here without having
    to know the difference between soft cancel and abandon."""
    from sali.runtime.revocation import revoke_intent

    pool = await _kernel(request).pool()
    result = await revoke_intent(pool, task_id, reason="user_cancelled",
                                 by=ident.name, publisher=_publisher(request))
    return {"task_id": str(task_id), "status": "cancelled", **result}


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
    """The skills selected (as durable snapshots) for a task (Prompt 5 §27) + composer metadata
    (kind/reason/detected_version) so a diagnostics UI can show WHY each skill was picked. Content
    body omitted here for size — the /skills/{name} endpoint returns full detail."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.skills.store import SkillStore

    rows = await SkillStore(pool).for_task(task_id)
    return {"task_id": str(task_id), "skills": [
        {"name": r["name"], "path": r["path"], "content_hash": r["content_hash"],
         "score": r["score"], "kind": r.get("kind"), "reason": r.get("reason"),
         "detected_version": r.get("detected_version")} for r in rows]}


@router.get("/skills")
async def list_skills(request: Request) -> dict[str, Any]:
    """Every skill available on this Sali, with live proficiency + version hint. Bodies OMITTED —
    call /skills/{name} for the full frontmatter + sections + text."""
    from pathlib import Path as _Path
    from sali.config.settings import load_settings
    from sali.skills.discovery import discover_skills
    from sali.skills.proficiency import compute_all

    kernel = _kernel(request)
    pool = await kernel.pool()
    root = _Path(load_settings().permissions.skills_root)
    skills = discover_skills(root)
    proficiency = {p.name: p.to_dict() for p in await compute_all(pool)}
    return {"skills": [
        {"name": s.name, "title": s.title, "tags": s.tags, "summary": s.summary,
         "sections": list(s.sections.keys()), "dependencies": s.dependencies,
         "conflicts": s.conflicts, "version_hint": s.version_hint,
         "project_detect": s.project_detect,
         "proficiency": proficiency.get(s.name)}
        for s in skills]}


@router.get("/skills-proficiency")
async def skills_proficiency(request: Request) -> dict[str, Any]:
    """Evidence-based per-skill proficiency (success/failure aggregated from task reviews)."""
    from sali.skills.proficiency import compute_all

    kernel = _kernel(request)
    pool = await kernel.pool()
    return {"skills": [p.to_dict() for p in await compute_all(pool)]}


@router.get("/skills/{name}")
async def get_skill(name: str, request: Request) -> dict[str, Any]:
    """Full detail for one skill — parsed frontmatter + summary + section bodies + proficiency."""
    from pathlib import Path as _Path
    from sali.config.settings import load_settings
    from sali.skills.discovery import discover_skills
    from sali.skills.proficiency import compute_for

    kernel = _kernel(request)
    pool = await kernel.pool()
    root = _Path(load_settings().permissions.skills_root)
    skills = {s.name: s for s in discover_skills(root)}
    s = skills.get(name.lower())
    if s is None:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    prof = await compute_for(pool, s.name)
    return {"name": s.name, "title": s.title, "tags": s.tags, "content_hash": s.content_hash,
            "summary": s.summary, "sections": s.sections, "content": s.content,
            "dependencies": s.dependencies, "conflicts": s.conflicts,
            "version_hint": s.version_hint, "project_detect": s.project_detect,
            "proficiency": prof.to_dict()}


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


@router.get("/current-state")
async def get_current_state(request: Request) -> dict[str, Any]:
    """§36-38: ONE authoritative read of what's true right now — identity, environment, how Almir is
    connected, mode + current task, last outcome, open commitments, and grounding health — with every
    fact TAGGED by the authority tier it came from (observed > state > recall > belief) and the
    precedence Sali uses when sources disagree. Composes existing stores; writes nothing."""
    pool = await _kernel(request).pool()
    from sali.runtime.current_state import CurrentState

    conn = getattr(getattr(_runtime(request), "_loop", None), "_connection", None)
    return await CurrentState(pool, connection=conn).snapshot()


@router.get("/grounding")
async def get_grounding(request: Request, limit: int = 20) -> dict[str, Any]:
    """Sali's grounding sink (§45): how often his OWN over-claims were caught and struck before reaching
    Almir — lifetime, last 24h, and by family (action_done/state/capability/file_send) — plus the most
    recent struck sentences for review (§32/§44). Low and trending-down is the health signal: a
    self-caught contradiction is a limit Sali is holding to, recorded so it can be learned from."""
    pool = await _kernel(request).pool()
    from sali.runtime.grounding_log import GroundingLog

    log = GroundingLog(pool)
    return {"metrics": await log.metrics(), "recent": await log.recent(limit=limit)}


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


@router.get("/memory/list")
async def list_memory(
    request: Request,
    layer: str | None = None, source: str | None = None, kind: str | None = None,
    scope: str | None = None, q: str | None = None, state: str = "current",
    needs_grounding: bool | None = None, min_confidence: float | None = None,
    limit: int = 50, cursor_created_at: str | None = None, cursor_id: UUID | None = None,
) -> dict[str, Any]:
    """Browse memory. Keyset paginated — no offset, no total (a total is unbounded; see /memory/stats)."""
    from datetime import datetime

    from sali.memory import admin

    cur = datetime.fromisoformat(cursor_created_at) if cursor_created_at else None
    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        return await admin.list_memories(
            conn, layer=layer, source=source, kind=kind, scope=scope, q=q, state=state,
            needs_grounding=needs_grounding, min_confidence=min_confidence, limit=limit,
            cursor_created_at=cur, cursor_id=cursor_id)


@router.get("/memory/stats")
async def memory_stats(request: Request) -> dict[str, Any]:
    """Counts for the memory overview — by layer, by source, and the review queues. Never content."""
    from sali.memory import admin

    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        return await admin.stats(conn)


@router.post("/memory/{memory_id}/invalidate")
async def invalidate_memory(
    memory_id: UUID, request: Request, reason: str = "",
    _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Retire a belief from current knowledge. REVERSIBLE — the row and its history stay."""
    from sali.memory import writer

    if not reason.strip():
        raise HTTPException(status_code=400, detail="reason cannot be empty")
    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        closed = await writer.forget(conn, memory_id, reason=reason.strip())
    if not closed:
        raise HTTPException(status_code=404, detail="memory not found or already retired")
    return {"invalidated": True, "memory_id": str(memory_id)}


@router.post("/memory/{memory_id}/restore")
async def restore_memory(
    memory_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Bring a retired memory back. Refused for a SUPERSEDED row — see memory/admin.restore."""
    from sali.memory import admin

    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        out = await admin.restore(conn, memory_id)
    if not out["restored"]:
        code = 404 if out["reason"] == "not_found" else 409
        raise HTTPException(status_code=code, detail=out["reason"])
    return out


@router.post("/memory/{memory_id}/revise")
async def revise_memory(
    memory_id: UUID, request: Request, content: str = "", reason: str = "",
    _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Correct what a memory says. Retires the old row and writes the replacement, linked."""
    from sali.memory import admin

    if not content.strip():
        raise HTTPException(status_code=400, detail="content cannot be empty")
    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        out = await admin.revise(conn, memory_id, content=content.strip(),
                                 reason=reason.strip() or "corrected by Almir")
    if not out["revised"]:
        raise HTTPException(status_code=404 if out["reason"] == "not_found" else 409,
                            detail=out["reason"])
    return out


@router.delete("/memory/{memory_id}")
async def delete_memory(
    memory_id: UUID, request: Request, reason: str = "", confirm: bool = False,
    identity: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Erase a memory outright. IRREVERSIBLE — for a privacy request, not a correction.

    Owner only, and `confirm=true` is required: a slip of the thumb must not erase something. The auth
    layer ranks owner above controller but only exposes `require_controller`, so the owner check is made
    here against the identity that dependency already resolved.
    """
    from sali.memory import admin

    if identity.role != "owner":
        raise HTTPException(status_code=403, detail="owner authority required")
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required to delete a memory")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="reason cannot be empty")
    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        out = await admin.hard_delete(conn, memory_id, reason=reason.strip())
    if not out["deleted"]:
        raise HTTPException(status_code=404, detail="memory not found")
    return out


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

@router.get("/agenda")
async def get_agenda(request: Request) -> dict[str, Any]:
    """The synthesised cross-store view of what matters now / today / upcoming / blocked - drawn
    from live task, commitment, schedule, goal, initiative rows. Read-only, bounded (default 5 per
    section), timezone-correct against the owner's calendar day. This is what the iPhone Agenda
    screen reads and what the chat path 'what is on my agenda' should answer from.

    Never invents structure: an empty store means an empty section, not a placeholder. The same
    reader is called from the loop's context assembly, so chat and app can never disagree about
    what is on the agenda - they read the same synthesiser at the same moment."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    from sali.core.temporal import TemporalService
    from sali.tasks.agenda import AgendaSynthesiser

    view = await AgendaSynthesiser(
        pool,
        TemporalService(owner_timezone=kernel.settings.temporal.owner_timezone)
    ).synthesise()
    return view.to_json()


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


@router.get("/capabilities/detail")
async def get_capability_detail(
    request: Request, name: str, scope: str = "environment", scope_ref: str | None = None,
) -> dict[str, Any]:
    """One capability with the evidence behind it: whether it is usable RIGHT NOW, and the real record
    of it being used (§12/§13). `availability` is the honest answer to "can Sali do this today" —
    'available' only when it is both usable and recently verified, 'stale' when it has not been proven
    in a while, 'degraded' when it has been failing. The usage log is what separates a competence from
    a claim: a capability with no uses has never actually been exercised."""
    pool = await _kernel(request).pool()
    from sali.learning.capability import CapabilityStore

    store = CapabilityStore(pool)
    cap = await store.get(name=name, scope=scope, scope_ref=scope_ref)
    if cap is None:
        raise HTTPException(status_code=404, detail="capability not found")
    return {
        "capability": cap,
        "availability": await store.check_availability(name=name, scope=scope, scope_ref=scope_ref),
        "usable_now": await store.is_usable(name=name, scope_ref=scope_ref),
        "usage": await store.usage_history(name=name, scope=scope, scope_ref=scope_ref, limit=20),
    }


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


@router.get("/open-loops")
async def get_open_loops(request: Request) -> dict[str, Any]:
    """Unresolved matters Sali is holding mental space for (persistent-organism §6). Distinct
    from initiatives (action candidates), goals (aspirations), commitments (promised). Sorted
    by priority desc, then last_touched desc. Iterable as 'what's on Sali's mind' in the app."""
    pool = await _kernel(request).pool()
    from sali.tasks.open_loops import OpenLoopStore

    loops = await OpenLoopStore(pool).open_loops(limit=50)
    return {"open_loops": [{
        "id": str(r["id"]), "title": r["title"], "description": r.get("description") or "",
        "kind": r["kind"], "source": r["source"], "priority": r["priority"],
        "status": r["status"],
        "last_touched_at": r["last_touched_at"].isoformat() if r["last_touched_at"] else None,
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
    } for r in loops]}


@router.get("/curiosities")
async def get_curiosities(request: Request) -> dict[str, Any]:
    """Currently-open knowledge gaps (§5). Sorted by priority + volume; a high-priority
    curiosity is a legitimate reason for idle-time background research."""
    pool = await _kernel(request).pool()
    from sali.learning.curiosity import CuriosityStore

    rows = await CuriosityStore(pool).open_curiosities(limit=50)
    return {"curiosities": [{
        "id": str(r["id"]), "subject": r["subject"], "statement": r["statement"],
        "why_it_matters": r["why_it_matters"],
        "current_understanding": r["current_understanding"],
        "priority": r["priority"], "times_encountered": r["times_encountered"],
        "last_encountered_at": (r["last_encountered_at"].isoformat()
                                 if r["last_encountered_at"] else None),
        "status": r["status"], "remaining_interesting": r["remaining_interesting"],
        "discoveries": r["discoveries"] or [],
    } for r in rows]}


@router.get("/proactive-decisions")
async def get_proactive_decisions(request: Request, limit: int = 100) -> dict[str, Any]:
    """Audit trail of proactive communication — every decision (sent AND suppressed) with
    reason codes. Feeds §46 'learn when NOT to speak': aggregate to identify categories that
    get no engagement and rate them down for future decisions."""
    pool = await _kernel(request).pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, kind, subject_ref, decision, reason_codes, message, "
            "engagement, engagement_at, decided_at FROM sali.proactive_decision "
            "ORDER BY decided_at DESC LIMIT $1",
            min(max(1, limit), 500))
    return {"decisions": [{
        "id": str(r["id"]), "kind": r["kind"], "subject_ref": r["subject_ref"],
        "decision": r["decision"], "reason_codes": r["reason_codes"],
        "message": r["message"], "engagement": r["engagement"],
        "engagement_at": (r["engagement_at"].isoformat() if r["engagement_at"] else None),
        "decided_at": (r["decided_at"].isoformat() if r["decided_at"] else None),
    } for r in rows]}


@router.get("/cognitive-metrics")
async def get_cognitive_metrics(request: Request, hours: int = 24) -> dict[str, Any]:
    """Observability for the cognitive-cycle driver (§36) — is Sali actually thinking?

    Reports: driver last-cycle timestamp + count, decision-trace rollup by mode/outcome,
    initiative row count, proactive-decision row count. All from cheap aggregate queries.
    Use `hours` to window the trace rollup (default 24h)."""
    from datetime import UTC, datetime, timedelta

    from sali.cognitive.decision_trace import DecisionTraceStore

    kernel = _kernel(request)
    pool = await kernel.pool()

    driver_status: dict[str, Any] = {"last_cycle_at": None, "last_cycle_count": 0}
    driver = getattr(kernel, "_initiative_driver", None)
    if driver is not None:
        try:
            driver_status = await driver.status()
        except Exception:  # noqa: BLE001
            pass

    trace = DecisionTraceStore(pool)
    since = datetime.now(UTC) - timedelta(hours=max(1, min(hours, 168)))
    rollup = await trace.rollup(since=since)

    async with pool.acquire() as conn:
        init_count = await conn.fetchval(
            "SELECT count(*) FROM sali.initiative WHERE created_at >= $1", since)
        proactive_count = await conn.fetchval(
            "SELECT count(*) FROM sali.proactive_decision WHERE decided_at >= $1", since)
        open_loops = await conn.fetchval(
            "SELECT count(*) FROM sali.open_loop WHERE status = 'open'")
        open_curiosities = await conn.fetchval(
            "SELECT count(*) FROM sali.curiosity WHERE status = 'open'")

    return {
        "window_hours": hours,
        "driver": driver_status,
        "decisions": rollup,
        "initiatives_touched": int(init_count or 0),
        "proactive_decisions": int(proactive_count or 0),
        "open_loops": int(open_loops or 0),
        "open_curiosities": int(open_curiosities or 0),
    }


@router.get("/decision-trace")
async def get_decision_trace(request: Request, limit: int = 100) -> dict[str, Any]:
    """The compact per-decision metadata the cognitive layer records (§29/§44). NOT
    chain-of-thought — structured reason codes + evidence keys only."""
    from sali.cognitive.decision_trace import DecisionTraceStore

    pool = await _kernel(request).pool()
    trace = DecisionTraceStore(pool)
    rows = await trace.recent(limit=limit)
    return {"traces": [{
        "id": str(r["id"]), "mode": r["mode"], "subject_ref": r["subject_ref"],
        "origin": r["origin"], "reason_codes": r["reason_codes"],
        "confidence": r["confidence"], "evidence": r["evidence"],
        "expected_outcome": r["expected_outcome"],
        "actual_outcome": r["actual_outcome"],
        "outcome_at": (r["outcome_at"].isoformat() if r["outcome_at"] else None),
        "policy_result": r["policy_result"],
        "learning_id": (str(r["learning_id"]) if r["learning_id"] else None),
        "decided_at": (r["decided_at"].isoformat() if r["decided_at"] else None),
    } for r in rows]}


@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    """Installed models the owner can switch Sali to, each with its capabilities (vision/tools) and
    the active one flagged. Powers the in-app model switcher (Settings). Reads Ollama's /api/tags for
    the list and /api/show for per-model capabilities — the authoritative source, so 'vision' here is
    exactly what makes the chat image button appear or disappear."""
    import httpx

    kernel = _kernel(request)
    host = kernel.settings.model.host
    embed = (kernel.settings.model.embed_model or "").lower()
    active = getattr(kernel.provider, "chat_model", None) or kernel.settings.model.chat_model
    models: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(base_url=host, timeout=15.0) as cx:
            tags = (await cx.get("/api/tags")).json().get("models", []) or []
            for m in tags:
                name = m.get("name") or m.get("model") or ""
                if not name or "embed" in name.lower() or name.lower() == embed:
                    continue  # the embedder is not a chat model — never offer it
                caps: list[str] = []
                with contextlib.suppress(Exception):
                    info = (await cx.post("/api/show", json={"model": name})).json()
                    caps = info.get("capabilities") or []
                det = m.get("details", {}) or {}
                models.append({
                    "name": name, "size": m.get("size"),
                    "family": det.get("family"),
                    "parameter_size": det.get("parameter_size"),
                    "quantization": det.get("quantization_level"),
                    "vision": "vision" in caps, "tools": "tools" in caps,
                    "active": name == active,
                })
    except Exception as exc:  # noqa: BLE001 — never 500 the switcher; report what we can
        raise HTTPException(status_code=502, detail=f"could not reach the model host: {exc}")
    return {"models": models, "active": active}


@router.post("/model")
async def set_model(request: Request, payload: dict[str, Any],
                    _: Identity = Depends(require_controller)) -> dict[str, Any]:
    """Switch Sali's active chat model (owner-only). Validates it is installed + tool-capable, points
    the provider at it, persists the choice (survives restart), and broadcasts model.changed over the
    WebSocket so the app reacts live — in particular the chat image button appears/disappears with the
    new model's vision capability. Refused while Sali is busy (a swap reloads VRAM)."""
    import httpx

    kernel = _kernel(request)
    runtime = _runtime(request)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="model name required")
    # Never swap mid-work — reloading the model into VRAM would disrupt a running turn/task.
    if runtime is not None and getattr(runtime, "_coordinator", None) is not None:
        with contextlib.suppress(Exception):
            if runtime._coordinator.is_busy:
                raise HTTPException(status_code=409,
                                   detail="Sali is busy right now — switch when idle")
    # Validate against the model host + read real capabilities.
    host = kernel.settings.model.host
    try:
        async with httpx.AsyncClient(base_url=host, timeout=20.0) as cx:
            r = await cx.post("/api/show", json={"model": name})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not reach the model host: {exc}")
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail=f"model '{name}' is not installed")
    caps = r.json().get("capabilities") or []
    vision, tools = "vision" in caps, "tools" in caps
    if not tools:
        raise HTTPException(
            status_code=422,
            detail=f"'{name}' has no tool-calling — Sali needs tools to work, so it can't be used")
    # Point the provider at it + persist the choice.
    with contextlib.suppress(Exception):
        kernel.provider.set_active_model(name)
    # CRITICAL on a 12GB card: evict the model we just switched AWAY from (and any stray) so two 30B
    # MoEs never sit in VRAM together. Ollama holds up to 2 models and Sali pins keep_alive=24h, so
    # without this the old model lingers. The new one loads on the next turn into the freed VRAM.
    unloaded: list[str] = []
    with contextlib.suppress(Exception):
        if hasattr(kernel.provider, "ensure_only_loaded"):
            unloaded = await kernel.provider.ensure_only_loaded(name)
    pool = await kernel.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sali.sali_state SET active_chat_model = $1 WHERE id = true", name)
    # Broadcast live so the app updates the active model + shows/hides the image button immediately.
    if runtime is not None:
        with contextlib.suppress(Exception):
            await runtime._publisher.emit(
                event_type="model.changed", subject_type="model", origin="system",
                data={"name": name, "vision": vision, "tools": tools})
    return {"active": name, "vision": vision, "tools": tools, "unloaded": unloaded}


@router.get("/routines")
async def get_routines(request: Request) -> dict[str, Any]:
    """Recurring activities Sali maintains, with their next fire time + failure history (§8)."""
    pool = await _kernel(request).pool()
    from sali.tasks.routines import RoutineStore

    store = RoutineStore(pool)
    return {"routines": await store.list(), "counts": await store.counts()}


@router.post("/routines")
async def create_routine(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create (or refresh) a recurring activity. Body: {name, when, purpose?, conditions?}
    where `when` is an interval spec like '30m' / '2h' / '1d'. Deduped by name — a repeat
    call with the same name updates purpose/schedule instead of creating a duplicate.
    Previously RoutineStore.create had no external caller; the routine table stayed empty."""
    pool = await _kernel(request).pool()
    from sali.tasks.routines import RoutineStore

    name = str(payload.get("name") or "").strip()
    when = str(payload.get("when") or "").strip()
    if not name or not when:
        raise HTTPException(status_code=400, detail="name and when are required")
    store = RoutineStore(pool)
    try:
        rid = await store.create(
            name=name[:120], when=when,
            purpose=(str(payload["purpose"])[:400] if payload.get("purpose") else None),
            conditions=payload.get("conditions") or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"routine_id": str(rid), "name": name}


@router.get("/skill-proposals")
async def list_skill_proposals(request: Request, limit: int = 50) -> dict[str, Any]:
    """Proposed improvements to a skill's guidance, awaiting human review (Prompt 7 §14/§15).
    Previously the SkillProposalStore had zero API surface; nothing outside the class file
    called propose/accept/reject."""
    pool = await _kernel(request).pool()
    from sali.learning.skill_evolution import SkillProposalStore

    store = SkillProposalStore(pool)
    return {"proposals": await store.pending(limit=min(max(1, limit), 200))}


@router.post("/skill-proposals")
async def create_skill_proposal(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Propose an improvement to a skill's guidance. Body: {skill_name, proposed_change,
    current_guidance?, reason?, times_successful?, times_failed?, confidence?}.
    Deduped per (skill_name, LOWER(proposed_change))."""
    pool = await _kernel(request).pool()
    from sali.learning.skill_evolution import SkillProposalStore

    skill_name = str(payload.get("skill_name") or "").strip()
    proposed = str(payload.get("proposed_change") or "").strip()
    if not skill_name or not proposed:
        raise HTTPException(status_code=400, detail="skill_name and proposed_change are required")
    store = SkillProposalStore(pool)
    pid = await store.propose(
        skill_name=skill_name[:120], proposed_change=proposed[:2000],
        current_guidance=(str(payload["current_guidance"])[:4000]
                          if payload.get("current_guidance") else None),
        reason=(str(payload["reason"])[:1000] if payload.get("reason") else None),
        evidence=payload.get("evidence") or {},
        times_successful=int(payload.get("times_successful") or 0),
        times_failed=int(payload.get("times_failed") or 0),
        confidence=float(payload.get("confidence") or 0.4))
    return {"proposal_id": str(pid), "skill_name": skill_name}


@router.post("/skill-proposals/{proposal_id}/accept")
async def accept_skill_proposal(request: Request, proposal_id: str,
                                  payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from uuid import UUID as _UUID
    from sali.learning.skill_evolution import SkillProposalStore

    pool = await _kernel(request).pool()
    by = str((payload or {}).get("by") or "user")
    ok = await SkillProposalStore(pool).accept(_UUID(proposal_id), by=by)
    return {"accepted": bool(ok)}


@router.post("/skill-proposals/{proposal_id}/reject")
async def reject_skill_proposal(request: Request, proposal_id: str,
                                  payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from uuid import UUID as _UUID
    from sali.learning.skill_evolution import SkillProposalStore

    pool = await _kernel(request).pool()
    body = payload or {}
    ok = await SkillProposalStore(pool).reject(
        _UUID(proposal_id), by=str(body.get("by") or "user"),
        reason=str(body.get("reason") or "")[:500])
    return {"rejected": bool(ok)}


@router.post("/proactive-decisions/{decision_id}/engagement")
async def record_engagement(request: Request, decision_id: str,
                              payload: dict[str, Any]) -> dict[str, Any]:
    """Score a proactive decision — Almir tapped, read, replied, or ignored it. Feeds the
    §46 learn-when-not-to-speak gate. Previously CommunicationDecisionEngine.record_engagement
    was defined but had ZERO callers; the engagement column stayed null forever."""
    from sali.events.communication_decision import CommunicationDecisionEngine

    engagement = str(payload.get("engagement") or "").strip().lower()
    if engagement not in ("none", "read", "replied", "acted"):
        raise HTTPException(status_code=400,
                            detail="engagement must be one of: none, read, replied, acted")
    pool = await _kernel(request).pool()
    engine = CommunicationDecisionEngine(pool)
    await engine.record_engagement(decision_id, engagement=engagement)
    return {"decision_id": decision_id, "engagement": engagement}


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


@router.post("/tasks/{task_id}/question/answer")
async def answer_task_question(
    task_id: UUID, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Answer a task's clarification question, putting the SAME task back to 'running' (§44).

    Distinct from /pending-questions/answer, which serves the person-directed `pending_question`
    table: a task clarification lives in `task_question` (QuestionStore) and had no HTTP route at
    all, so answering one from the app always came back 'no_pending_question'."""
    from sali.tasks.coordination import QuestionStore

    pool = await _kernel(request).pool()
    body = await request.json()
    answer = str(body.get("answer", "") or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer cannot be empty")
    answered = await QuestionStore(pool, _publisher(request)).answer(task_id, answer)
    if not answered:
        raise HTTPException(status_code=404, detail="no pending question for this task")
    return {"status": "answered", "task_id": str(task_id)}


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


@router.post("/schedules", response_model=ScheduleResponse)
async def create_schedule(
    body: ScheduleCreateRequest, request: Request, _: Identity = Depends(require_controller),
) -> ScheduleResponse:
    """Create a schedule (terminal parity: the store always supported it; only the route was missing)."""
    from sali.scheduler.store import ScheduleStore

    pool = await _kernel(request).pool()
    try:
        s = await ScheduleStore(pool).create(body.name, body.when, body.prompt)
    except Exception as exc:  # noqa: BLE001 - a bad spec is a client error, never a 500
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from None
    return ScheduleResponse(
        id=s.id, name=s.name, kind=s.kind, spec=s.spec, prompt=s.prompt,
        enabled=s.enabled, next_run_at=s.next_run_at, last_run_at=s.last_run_at,
        last_status=s.last_status)


@router.post("/schedules/{name}/enable")
async def enable_schedule(
    name: str, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    from sali.scheduler.store import ScheduleStore

    pool = await _kernel(request).pool()
    n = await ScheduleStore(pool).set_enabled(name, True)
    if n == 0:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"name": name, "enabled": True}


@router.post("/schedules/{name}/disable")
async def disable_schedule(
    name: str, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    from sali.scheduler.store import ScheduleStore

    pool = await _kernel(request).pool()
    n = await ScheduleStore(pool).set_enabled(name, False)
    if n == 0:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"name": name, "enabled": False}


@router.delete("/schedules/{name}")
async def delete_schedule(
    name: str, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    from sali.scheduler.store import ScheduleStore

    pool = await _kernel(request).pool()
    n = await ScheduleStore(pool).delete(name)
    if n == 0:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"name": name, "deleted": True}


# ── Memory (terminal parity: `sali remember` / `sali recall`) ─────────────────

@router.post("/memory")
async def remember_fact(
    body: RememberRequest, request: Request, _: Identity = Depends(require_controller),
) -> dict[str, Any]:
    """Teach Sali a fact from the app — persisted with provenance user_explicit and embedded."""
    import contextlib

    from sali.core.enums import MemoryLayer, MemorySource
    from sali.memory.service import MemoryService

    kernel = _kernel(request)
    pool = await kernel.pool()
    try:
        layer = MemoryLayer(body.layer)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown layer '{body.layer}'") from None
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="content cannot be empty")
    service = MemoryService(pool, kernel.provider)
    memory = await service.remember(layer=layer, content=body.content.strip(),
                                    source=MemorySource.USER_EXPLICIT)
    with contextlib.suppress(Exception):
        await service.embed_pending()
    return {"id": str(getattr(memory, "id", "")), "layer": body.layer,
            "confidence": float(getattr(memory, "confidence", 0.0) or 0.0)}


@router.get("/recall")
async def recall_memories(q: str, request: Request) -> dict[str, Any]:
    """What Sali retrieves for a query — memories (confidence/staleness) + graph facts."""
    from sali.retrieval.router import classify as _classify
    from sali.retrieval.service import RetrievalService

    kernel = _kernel(request)
    pool = await kernel.pool()
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")
    plan = _classify(q)
    bundle = await RetrievalService(
        pool, kernel.provider,
        owner_timezone=kernel.settings.temporal.owner_timezone).gather(q, plan, k=8)
    return {
        "intent": str(getattr(plan, "intent", "")),
        "memories": [{
            "content": h.memory.content,
            "confidence": round(float(h.effective_confidence), 3),
            "stale": bool(getattr(h, "stale", False)),
            "layer": str(getattr(h.memory, "layer", "")),
        } for h in bundle.memories],
        "graph": [{"src": f.src, "rel": f.rel, "dst": f.dst} for f in bundle.graph_facts],
    }


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


@router.get("/tasks/{task_id}/events")
async def get_task_events(
    task_id: UUID,
    request: Request,
    after_seq: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Task-scoped durable event log for the iOS pulse seed on TaskDetailView. Was: mobile
    fetched the global /events (last 200 rows) and filtered client-side by task_id, so on a
    busy backend a task whose events fell outside the 200-row window silently disappeared.
    Now: a UNION on (subject_id = task_id) OR (payload->>'task_id' = task_id::text) so both
    direct-subject events and dependent-work events (obligations, commitments, side effects
    that carry the task_id in their payload) are returned bounded to THIS task's history."""
    kernel = _kernel(request)
    pool = await kernel.pool()
    limit = max(1, min(limit, 500))
    tid_text = str(task_id)

    async with pool.acquire() as conn:
        if after_seq is not None:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                "FROM event WHERE seq > $1 "
                "  AND ((subject_type = 'task' AND subject_id::text = $2) "
                "        OR payload->>'task_id' = $2) "
                "ORDER BY seq LIMIT $3",
                after_seq, tid_text, limit)
        else:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                "FROM event WHERE (subject_type = 'task' AND subject_id::text = $1) "
                "  OR payload->>'task_id' = $1 "
                "ORDER BY seq DESC LIMIT $2", tid_text, limit)
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
    # Turn 7: compute step-count vocabulary from the in-memory steps list. This
    # matches the SQL predicate in _compute_tally exactly, so the row on the wire
    # is byte-identical to what the store computes for the event bus.
    _steps = list(task.steps or [])
    _done  = sum(1 for s in _steps if s.status == "done")
    _skip  = sum(1 for s in _steps if s.status == "skipped")
    _verif = sum(1 for s in _steps if s.status == "done" and getattr(s, "verified", False))
    return TaskResponse(
        done_steps=_done,
        total_steps=len(_steps),
        verified_steps=_verif,
        skipped_steps=_skip,
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
                checkpoint=s.checkpoint, parent_seq=s.parent_seq,
                definition_of_done=s.definition_of_done, scope_excludes=s.scope_excludes,
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
