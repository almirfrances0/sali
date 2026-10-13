"""API authentication & authorization (Prompt 13 §22-§28).

Two credential kinds resolve to one `Identity`:

  • DEVICE  — a short-lived access token issued to an enrolled iPhone (see devices.py). This is what the
              app uses for every REST/WS request. Revocable and expiring; carries a role (owner/controller/
              observer). This is the normal path.

  • HOST    — the local API token on disk (0600, never leaves the machine). It represents the machine's
              owner and is used ONLY to bootstrap enrollment (mint a pairing code) and as local break-glass.
              The iPhone never receives it.

Authentication answers "who is this?"; authorization ("what may they do?") is the role ladder
owner ⊃ controller ⊃ observer. Observers are read-only. Every mutating route depends on `require_controller`;
the whole data router depends on `require_identity`, so unauthenticated access is rejected (§43).

Security: tokens are never logged (only a 4-char prefix for identification); the host token is compared in
constant time; device tokens are matched by SHA-256 lookup against durable, expiring, revocable sessions.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from sali.api.devices import DeviceStore
from sali.obs.log import get_logger

log = get_logger("sali.api.auth")

_TOKEN_FILE = Path.home() / ".config" / "sali" / "api_token"

# Role ordering for authorization checks (§27).
_ROLE_RANK = {"observer": 0, "controller": 1, "owner": 2}


def get_or_create_token() -> str:
    """Read the host API token from disk, or create one. Host-only; the iPhone never receives this."""
    if _TOKEN_FILE.exists():
        token = _TOKEN_FILE.read_text().strip()
        if len(token) >= 16:  # sanity check
            return token
    token = secrets.token_urlsafe(32)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token)
    _TOKEN_FILE.chmod(0o600)
    log.info("api_token_created", prefix=token[:4] + "...")  # only the prefix — never the full token
    return token


def verify_token(provided: str | None, expected: str) -> bool:
    """Constant-time comparison. Returns False if provided is None or empty."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided.encode(), expected.encode())


@dataclass(frozen=True)
class Identity:
    """Who is behind a request. `kind` is 'device' or 'host'; `device_id` is set only for devices."""
    kind: str
    role: str
    name: str
    device_id: Any = None  # UUID for devices, None for host

    def at_least(self, role: str) -> bool:
        return _ROLE_RANK.get(self.role, -1) >= _ROLE_RANK.get(role, 99)


def bearer_token(request: Request) -> str | None:
    """Extract a bearer token from the Authorization header ONLY.

    REST routes must send `Authorization: Bearer <token>` — the `?token=` query form is NOT accepted here
    (it would leak short-lived access tokens into tunnel/proxy access logs, browser history and Referer
    headers, Final audit §28). The WebSocket handshake reads its own `token` query param separately (some
    WS clients can't set headers); that path is in app.py, not this REST helper."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


async def authenticate(pool: Any, token: str | None) -> Identity | None:
    """Resolve a token to an Identity, or None. Tries a device session first, then the host token."""
    if not token:
        return None
    device = await DeviceStore(pool).authenticate(token)
    if device is not None:
        return Identity(kind="device", role=device.role, name=device.name, device_id=device.device_id)
    if verify_token(token, get_or_create_token()):
        return Identity(kind="host", role="owner", name="host")
    return None


async def _pool(request: Request) -> Any:
    kernel = request.app.state.kernel
    if kernel is None:  # pragma: no cover - defensive; the app is always created with a kernel in prod
        raise HTTPException(status_code=503, detail="runtime not ready")
    return await kernel.pool()


async def require_identity(request: Request) -> Identity:
    """FastAPI dependency: any authenticated identity, else 401. Applied to the whole data router (§43)."""
    ident = await authenticate(await _pool(request), bearer_token(request))
    if ident is None:
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": "Bearer"})
    return ident


async def require_controller(request: Request) -> Identity:
    """Dependency for mutating routes: owner or controller (observers are read-only, §27)."""
    ident = await require_identity(request)
    if not ident.at_least("controller"):
        raise HTTPException(status_code=403, detail="this device is read-only (observer)")
    return ident


async def require_owner(request: Request) -> Identity:
    """Dependency for device-management routes: owner only (§24/§27)."""
    ident = await require_identity(request)
    if not ident.at_least("owner"):
        raise HTTPException(status_code=403, detail="owner authority required")
    return ident
