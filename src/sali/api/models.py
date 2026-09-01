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

class ConversationMessage(BaseModel):
    id: UUID
    session_id: UUID
    seq: int
    role: str  # user | assistant | system
    content: str
    model: str | None = None
    created_at: datetime | None = None


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
    superseded_by: UUID | None = None


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


class SendMessageResponse(BaseModel):
    run_id: UUID
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
