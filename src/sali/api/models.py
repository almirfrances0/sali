"""API response models — the contract between Sali backend and mobile clients.

These Pydantic models define the exact JSON shape the iOS app receives.
Every field maps to real Sali state — no invented data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# ── Conversation ──────────────────────────────────────────────────────────────

class MessageAttachment(BaseModel):
    """A file Sali sent, carried on a conversation message so it renders as a downloadable card and
    survives a reload (durable delivery — a live-only card vanished on refresh)."""
    artifact_id: str
    filename: str
    size: int | None = None
    download_url: str
    kind: str | None = None


class ConversationMessage(BaseModel):
    id: UUID
    session_id: UUID
    seq: int
    role: str  # user | assistant | system
    content: str
    model: str | None = None
    created_at: datetime | None = None
    attachment: MessageAttachment | None = None


class ConversationState(BaseModel):
    session_id: UUID
    messages: list[ConversationMessage]
    summary: str | None = None
    last_active: datetime | None = None


# ── Agent Events (real-time stream) ───────────────────────────────────────────

class AgentEvent(BaseModel):
    """A single event from the agent loop — maps to LoopEvent kinds."""
    event_id: UUID
    type: str  # message.started | message.delta | tool.started | tool.completed | etc.
    timestamp: datetime
    task_id: UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ToolEvent(BaseModel):
    """Structured tool execution event."""
    event_id: UUID
    type: str  # tool.started | tool.completed | tool.failed
    timestamp: datetime
    tool: str
    summary: str | None = None
    ok: bool | None = None
    duration_ms: int | None = None
    task_id: UUID | None = None


# ── Tasks ─────────────────────────────────────────────────────────────────────

class TaskStepResponse(BaseModel):
    seq: int
    description: str
    status: str
    note: str | None = None
    attempts: int = 0
    last_error: str | None = None
    failure_class: str | None = None
    verified: bool = False
    checkpoint: dict[str, Any] | None = None
    # seq of this step's parent, or None for a top-level step. Lets the app render real sub-steps
    # instead of a flat list (migration 0045).
    parent_seq: int | None = None
    # Step-discipline (migration 0055): the step as a contract. definition_of_done is what "done"
    # concretely means for this step (the engine verifies it); scope_excludes is what this step must
    # NOT touch. The app shows these so the user can see the plan's real shape and the completion bar.
    definition_of_done: str | None = None
    scope_excludes: str | None = None


class TaskResponse(BaseModel):
    id: UUID
    objective: str
    status: str
    result: str | None = None
    is_primary: bool = False
    workspace_root: str | None = None
    workspace_mode: str = "none"
    steps: list[TaskStepResponse] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_heartbeat: datetime | None = None
    interrupted_at: datetime | None = None
    retry_count: int = 0
    max_retries: int = 3
    # WHAT SALI IS DOING RIGHT NOW. Almir: "in the app tasks are not realtime i can't see what sali
    # doing or where he's now". All of it was already recorded — `task_execution` has the tool, its
    # outcome and a one-line result; a running `agent_runs` row means a turn is in flight — and none of
    # it was served, so a task on iteration 13 looked exactly like a dead one: "1 step done", silence.
    working: bool = False
    current_step: int | None = None
    last_tool: str | None = None
    last_tool_status: str | None = None
    last_detail: str | None = None
    last_activity_at: datetime | None = None
    superseded_by: UUID | None = None
    # Turn 7: backend-authoritative step counts. iOS renders these verbatim so the
    # progress bar can never disagree with the auto-complete decision (both sourced
    # from _compute_tally in tasks/store.py). Optional for API-compat during rollout.
    done_steps: int | None = None
    total_steps: int | None = None
    verified_steps: int | None = None
    skipped_steps: int | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    active_task_id: UUID | None = None


# ── Schedules ─────────────────────────────────────────────────────────────────

class ScheduleResponse(BaseModel):
    id: UUID
    name: str
    kind: str  # cron | interval
    spec: str
    prompt: str
    enabled: bool
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None


# ── System ────────────────────────────────────────────────────────────────────

class SystemStatus(BaseModel):
    version: str
    model: str
    session_id: str
    active_task_id: UUID | None = None
    uptime_s: float | None = None
    memory_count: int | None = None
    task_count: int | None = None


class HealthResponse(BaseModel):
    status: str  # ok | degraded | error
    datastore: bool = False
    model: bool = False
    embedder: bool = False
    internet: bool = False
    detail: dict[str, str] = {}


# ── Messages ──────────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    content: str
    image_ref: str | None = None    # absolute path returned by POST /api/v1/files (bounded to the
                                    # conversation workspace); Sali describes it into the turn.
    image_b64: str | None = None    # optional inline fallback for a tiny image (JSON body stays < 1 MB)


class SendMessageResponse(BaseModel):
    # None by design. The turn is dispatched to the runtime AFTER this 202 returns, so the real run id
    # does not exist yet; the route used to invent one with new_id(), which clients then compared against
    # the runtime's actual run_id (published on agent.run / agent.token) and never matched — silently
    # defeating reconnect recovery. The run id now reaches clients only from the event stream, where it
    # is true.
    run_id: UUID | None = None
    status: str = "accepted"


# ── Device enrollment & sessions (§23-§24) ──────────────────────────────────────

class EnrollCodeRequest(BaseModel):
    """Owner mints a one-time pairing code (on the host, or from an already-trusted owner device)."""
    role: str = "owner"  # owner | controller | observer
    label: str | None = None


class EnrollCodeResponse(BaseModel):
    code: str            # display form (XXXXX-XXXXX); shown once, never persisted in plaintext
    role: str
    expires_at: datetime


class EnrollRequest(BaseModel):
    """A fresh device redeems a pairing code for its first session."""
    code: str
    name: str
    model: str | None = None
    platform: str = "ios"


class SessionResponse(BaseModel):
    """The plaintext tokens — returned exactly once, then only their hashes exist server-side."""
    device_id: UUID
    role: str
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


class RefreshRequest(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    """Password login (POST /auth/login). The password is the durable credential the app re-authenticates
    with; `name` identifies the phone so repeated logins reuse one owner device row."""
    password: str
    name: str = "iPhone"
    model: str | None = None
    platform: str = "ios"


class DeviceResponse(BaseModel):
    id: UUID
    name: str
    platform: str
    model: str | None = None
    role: str
    status: str          # active | revoked
    has_push: bool = False
    push_environment: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


class PushTokenRequest(BaseModel):
    token: str
    environment: str = "production"  # sandbox | production


class ScheduleCreateRequest(BaseModel):
    name: str
    when: str      # interval ('30m') or 5-field cron ('0 9 * * *')
    prompt: str


class RememberRequest(BaseModel):
    content: str
    layer: str = "semantic"   # semantic | preference | episodic | identity
