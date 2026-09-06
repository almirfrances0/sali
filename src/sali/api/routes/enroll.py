"""Enrollment, session refresh, and device management routes (Prompt 13 §23-§24).

These live on a SEPARATE router from the data API: redemption and refresh must be reachable by a device
that has no session yet (they carry their own secret in the body), while code-minting and device management
require owner authority. The data router (routes/api.py) is uniformly behind `require_identity`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

import time

from sali.api.auth import Identity, require_identity, require_owner
from sali.api.devices import DeviceStore
from sali.api.models import (
    DeviceResponse,
    EnrollCodeRequest,
    EnrollCodeResponse,
    EnrollRequest,
    LoginRequest,
    PushTokenRequest,
    RefreshRequest,
    SessionResponse,
)
from sali.api.password import verify_password

router = APIRouter(prefix="/api/v1")

# ── Login brute-force throttle ──────────────────────────────────────────────────
# The password may be short and the API is internet-exposed, so failed logins are capped per client. This
# is coarse (behind Cloudflare all requests share the edge IP, so it becomes a near-global cap) but a weak
# default password on an open endpoint warrants it; a correct login clears the client's failure count.
_LOGIN_MAX_FAILS = 10
_LOGIN_WINDOW_S = 300.0
_login_failures: dict[str, list[float]] = {}


def _login_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_recent_failures(request: Request) -> list[float]:
    key = _login_client_key(request)
    now = time.monotonic()
    kept = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_WINDOW_S]
    if kept:
        _login_failures[key] = kept
    else:
        _login_failures.pop(key, None)
    return kept


def _login_guard(request: Request) -> None:
    if len(_login_recent_failures(request)) >= _LOGIN_MAX_FAILS:
        raise HTTPException(status_code=429, detail="too many attempts — wait a few minutes and try again")


def _login_record_failure(request: Request) -> None:
    _login_failures.setdefault(_login_client_key(request), []).append(time.monotonic())


def _login_clear(request: Request) -> None:
    _login_failures.pop(_login_client_key(request), None)


async def _store(request: Request) -> DeviceStore:
    kernel = request.app.state.kernel
    if kernel is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="runtime not ready")
    return DeviceStore(await kernel.pool())


def _session_response(s: Any) -> SessionResponse:
    return SessionResponse(
        device_id=s.device_id, role=s.role, access_token=s.access_token,
        refresh_token=s.refresh_token, access_expires_at=s.access_expires_at,
        refresh_expires_at=s.refresh_expires_at)


# ── Enrollment ──────────────────────────────────────────────────────────────────

@router.post("/enroll/code", response_model=EnrollCodeResponse)
async def mint_enrollment_code(
    body: EnrollCodeRequest, request: Request, ident: Identity = Depends(require_owner),
) -> EnrollCodeResponse:
    """Owner mints a one-time pairing code (host token, or an already-trusted owner device). The code
    expires and is single-use; it never becomes a permanent credential (§23)."""
    if body.role not in ("owner", "controller", "observer"):
        raise HTTPException(status_code=400, detail="invalid role")
    code, expires_at = await (await _store(request)).mint_enrollment_code(
        role=body.role, label=body.label, created_by=ident.name)
    return EnrollCodeResponse(code=code, role=body.role, expires_at=expires_at)


@router.post("/enroll", response_model=SessionResponse)
async def enroll(body: EnrollRequest, request: Request) -> SessionResponse:
    """A fresh device redeems a pairing code for its first session (§23). No prior authority required —
    the code IS the one-time authority. An unknown / used / expired code is rejected uniformly (401)."""
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="device name required")
    session = await (await _store(request)).redeem_enrollment(
        body.code, name=body.name.strip(), model=body.model, platform=body.platform)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid or expired enrollment code")
    return _session_response(session)


@router.post("/auth/login", response_model=SessionResponse)
async def login(body: LoginRequest, request: Request) -> SessionResponse:
    """Authenticate with the owner PASSWORD and mint a session (password auth). No prior token needed — the
    password IS the authority. The app re-calls this with its stored password whenever a token expires or is
    revoked, so an expiring/revoked token can NEVER lock the owner out; a wrong password (401) is the only
    thing that stops it — which is exactly what `sali change-password` (rotate = log everyone out) causes.
    Throttled per client to blunt brute force against a possibly-weak password on an open endpoint."""
    _login_guard(request)
    store = await _store(request)
    if not verify_password(body.password, await store.get_password_hash()):
        _login_record_failure(request)
        raise HTTPException(status_code=401, detail="incorrect password")
    _login_clear(request)
    session = await store.login(name=body.name, model=body.model, platform=body.platform)
    return _session_response(session)


@router.post("/auth/refresh", response_model=SessionResponse)
async def refresh(body: RefreshRequest, request: Request) -> SessionResponse:
    """Rotate a refresh token for a new access + refresh pair (§24). The old session is revoked, so a
    replayed refresh token is rejected."""
    session = await (await _store(request)).refresh_session(body.refresh_token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid or expired refresh token")
    return _session_response(session)


# ── Device management (owner) ─────────────────────────────────────────────────────

@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(request: Request, ident: Identity = Depends(require_owner)) -> list[DeviceResponse]:
    """List enrolled devices and their authorization/health (§24). Owner only."""
    return [DeviceResponse(**d) for d in await (await _store(request)).list_devices()]


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    device_id: UUID, request: Request, ident: Identity = Depends(require_owner),
) -> dict[str, Any]:
    """Revoke a device — kills every session bound to it immediately (§24). Owner only."""
    ok = await (await _store(request)).revoke_device(device_id)
    if not ok:
        raise HTTPException(status_code=404, detail="device not found or already revoked")
    # Tear down any live WebSocket bound to this device immediately — revocation must stop the event
    # stream now, not whenever the socket next happens to be touched (Final audit §28).
    from sali.api.ws import manager as ws_manager
    closed = await ws_manager.disconnect_device(str(device_id))
    return {"device_id": str(device_id), "status": "revoked", "sockets_closed": closed}


@router.post("/devices/{device_id}/push-token")
async def register_push_token(
    device_id: UUID, body: PushTokenRequest, request: Request,
    ident: Identity = Depends(require_identity),
) -> dict[str, Any]:
    """Register/refresh a device's APNs push token (§11). A device may set its own; an owner may set any."""
    if ident.kind == "device" and str(ident.device_id) != str(device_id) and not ident.at_least("owner"):
        raise HTTPException(status_code=403, detail="cannot set another device's push token")
    ok = await (await _store(request)).set_push_token(device_id, body.token, body.environment)
    if not ok:
        raise HTTPException(status_code=404, detail="device not found")
    return {"device_id": str(device_id), "status": "registered", "environment": body.environment}
