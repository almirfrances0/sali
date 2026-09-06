"""Device enrollment, device-bound sessions, and device authorization (Prompt 13 §23-§27).

The security model, end to end:

    host (holds the local API token) ──mint──▶ one-time enrollment code (short TTL, single use)
        code is shown to Almir (typed or QR)         │
                                                     ▼
    fresh iPhone ──redeem code──▶ api_device row + device_session (access + refresh)
        access token  (short TTL, e.g. 15 min)  ── used on every REST/WS request
        refresh token (longer TTL, rotated)     ── exchanged for a new access token
        both stored in the iOS Keychain; on the server ONLY their SHA-256 hashes exist

Nothing here trusts the app binary: a stolen binary carries no authority, because authority lives in a
server-side device_session that the owner can revoke at any time. No plaintext secret is ever persisted —
enrollment codes and tokens exist as plaintext exactly once, in the response that issues them.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.api.devices")

# Lifetimes — deliberately short access, longer rotating refresh (§24). Documented in API_REFERENCE.md.
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=30)
ENROLLMENT_TTL = timedelta(minutes=10)

# Unambiguous alphabet for a human-typeable pairing code (no 0/O/1/I/L). 10 chars ≈ 50 bits of entropy,
# single-use + 10-minute expiry → ample for a one-time pairing on a single-user system, and QR-friendly.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_ROLES = ("owner", "controller", "observer")


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(secret: str) -> str:
    """SHA-256 hex of a high-entropy secret. Safe as a lookup key; the plaintext is never stored."""
    return hashlib.sha256(secret.encode()).hexdigest()


def _normalize_code(code: str) -> str:
    """Codes are shown grouped (XXXXX-XXXXX) and typed case-insensitively; normalize before hashing."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _new_code() -> tuple[str, str]:
    """Return (display_code, normalized) — display is grouped for readability, normalized is hashed."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}", raw


@dataclass(frozen=True)
class IssuedSession:
    """A freshly issued session. The plaintext tokens exist only here, only once."""
    device_id: UUID
    role: str
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


@dataclass(frozen=True)
class AuthedDevice:
    """The identity behind an authenticated request — derived from a live device_session."""
    device_id: UUID
    session_id: UUID
    role: str
    name: str


class DeviceStore:
    """Durable device enrollment + session lifecycle. Owns its own transactions over a pool."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── Enrollment codes (minted on the host) ───────────────────────────────────────────────────────

    async def mint_enrollment_code(
        self, *, role: str = "owner", label: str | None = None, created_by: str = "host",
        ttl: timedelta = ENROLLMENT_TTL,
    ) -> tuple[str, datetime]:
        """Mint a one-time pairing code. Returns (display_code, expires_at). Only the hash is stored."""
        if role not in _ROLES:
            raise ValueError(f"unknown role: {role}")
        display, normalized = _new_code()
        expires_at = _now() + ttl
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO enrollment_code (code_hash, role, label, created_by, expires_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                _hash(normalized), role, label, created_by, expires_at)
        log.info("enrollment_code_minted", role=role, expires_at=expires_at.isoformat())
        return display, expires_at

    async def redeem_enrollment(
        self, code: str, *, name: str, model: str | None = None, platform: str = "ios",
    ) -> IssuedSession | None:
        """Exchange a valid, unexpired, unused code for a device + its first session.

        Returns None if the code is unknown, already used, or expired. Single-use is enforced
        atomically (the UPDATE only matches a row that is still open and unexpired)."""
        code_hash = _hash(_normalize_code(code))
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE enrollment_code SET used_at = now() "
                "WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now() "
                "RETURNING id, role, label",
                code_hash)
            if row is None:
                check_row = await conn.fetchrow(
                    "SELECT id, used_at, expires_at FROM enrollment_code WHERE code_hash = $1",
                    code_hash)
                if check_row is None:
                    log.warning("enrollment_redeem_failed", reason="code_not_found")
                elif check_row["used_at"] is not None:
                    log.warning("enrollment_redeem_failed", reason="code_already_used",
                                used_at=check_row["used_at"].isoformat())
                elif check_row["expires_at"] <= _now():
                    log.warning("enrollment_redeem_failed", reason="code_expired",
                                expires_at=check_row["expires_at"].isoformat())
                else:
                    log.warning("enrollment_redeem_failed", reason="unknown")
                return None
            device_id = await conn.fetchval(
                "INSERT INTO api_device (name, platform, model, role) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                name, platform, model, row["role"])
            await conn.execute(
                "UPDATE enrollment_code SET device_id = $1 WHERE id = $2", device_id, row["id"])
            session = await self._issue(conn, device_id, row["role"], name)
        log.info("device_enrolled", device_id=str(device_id)[:8], role=session.role)
        return session

    # ── Sessions ─────────────────────────────────────────────────────────────────────────────────────

    async def _issue(self, conn: Any, device_id: UUID, role: str, name: str) -> IssuedSession:
        """Create a device_session inside an existing transaction and return the plaintext tokens once."""
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = _now()
        access_exp, refresh_exp = now + ACCESS_TTL, now + REFRESH_TTL
        await conn.execute(
            "INSERT INTO device_session "
            "(device_id, access_hash, refresh_hash, access_expires_at, refresh_expires_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            device_id, _hash(access), _hash(refresh), access_exp, refresh_exp)
        return IssuedSession(device_id=device_id, role=role, access_token=access, refresh_token=refresh,
                             access_expires_at=access_exp, refresh_expires_at=refresh_exp)

    # Grace window during which an ALREADY-ROTATED refresh token is accepted once more. The client
    # scenario: our refresh_session succeeded server-side, the row was rotated, the fresh tokens
    # were sent back — but the response was lost in transit (Cloudflare restart, LTE handoff, WS
    # reaper closed the socket between response bytes, whatever). The client still holds the OLD
    # refresh token. Without a grace window, their next attempt hits an already-rotated row, the
    # server returns 401, the iOS client wipes credentials and forces re-enrollment. This
    # exactly matches the "requires enrollment again after restart" complaint (§8).
    #
    # 90 s covers a generous LTE-handover + Cloudflare-reconnect budget without meaningfully
    # weakening the single-active-session invariant. If a genuine attacker replays a captured
    # refresh within the same 90 s, they'd still need to defeat every OTHER auth control (device
    # already active, in-flight session usage). And accepting a grace-window token immediately
    # invalidates ALL prior sessions on this device, so the presumed lost-response successor
    # session (if any) is retired — one clean session going forward.
    _REFRESH_GRACE_SECONDS = 90

    async def refresh_session(self, refresh_token: str) -> IssuedSession | None:
        """Rotate a refresh token → a new access + refresh pair (§24 + production fix).

        Normal path: refresh row is live and not-revoked → rotate atomically.

        Grace path (production fix): refresh row IS revoked but was rotated in the last
        `_REFRESH_GRACE_SECONDS`. Treat this as a client retrying after a lost response and
        re-issue a fresh session, revoking ALL prior sessions on the device so the presumed
        (never-received) successor is retired and only ONE session remains active.

        Returns None if the token is truly unknown / expired / device-revoked.
        """
        rhash = _hash(refresh_token)
        async with self._pool.acquire() as conn, conn.transaction():
            # Normal, non-revoked case first.
            row = await conn.fetchrow(
                "SELECT s.id, s.device_id, d.role, d.name, d.status "
                "FROM device_session s JOIN api_device d ON d.id = s.device_id "
                "WHERE s.refresh_hash = $1 AND NOT s.revoked AND s.refresh_expires_at > now() "
                "FOR UPDATE OF s",
                rhash)
            if row is not None and row["status"] == "active":
                await conn.execute(
                    "UPDATE device_session SET revoked = true, rotated_at = now() "
                    "WHERE id = $1", row["id"])
                session = await self._issue(conn, row["device_id"], row["role"], row["name"])
                log.info("session_refreshed", device_id=str(session.device_id)[:8])
                return session

            # Grace-window redelivery: the client is presenting a refresh token that was already
            # rotated recently. Accept it once more and re-issue, then invalidate every other
            # session on this device so the (presumed lost-response) successor is retired.
            grace_row = await conn.fetchrow(
                f"SELECT s.id, s.device_id, d.role, d.name, d.status "
                f"FROM device_session s JOIN api_device d ON d.id = s.device_id "
                f"WHERE s.refresh_hash = $1 AND s.revoked = true "
                f"  AND s.rotated_at > now() - INTERVAL '{self._REFRESH_GRACE_SECONDS} seconds' "
                f"  AND s.refresh_expires_at > now() "
                f"FOR UPDATE OF s",
                rhash)
            if grace_row is None or grace_row["status"] != "active":
                log.warning("refresh_failed")
                return None
            # Retire every other session on this device so single-active-session holds and a
            # captured mid-air token can never race a legitimate rotation. Force refresh_expires_at
            # to `now()` so those retired rows are ALSO excluded from the grace window (otherwise
            # setting a fresh rotated_at would inadvertently open a new grace window for them).
            await conn.execute(
                "UPDATE device_session "
                "SET revoked = true, rotated_at = COALESCE(rotated_at, now()), "
                "    refresh_expires_at = now() "
                "WHERE device_id = $1 AND NOT revoked", grace_row["device_id"])
            session = await self._issue(
                conn, grace_row["device_id"], grace_row["role"], grace_row["name"])
        log.info("session_grace_reissued", device_id=str(session.device_id)[:8])
        return session

    async def authenticate(self, access_token: str | None) -> AuthedDevice | None:
        """Resolve an access token to a device identity, or None. Rejects expired/revoked sessions and
        revoked devices; updates last_seen_at. This is the gate on every authenticated request."""
        if not access_token:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT s.id AS session_id, s.device_id, d.role, d.name "
                "FROM device_session s JOIN api_device d ON d.id = s.device_id "
                "WHERE s.access_hash = $1 AND NOT s.revoked AND s.access_expires_at > now() "
                "  AND d.status = 'active'",
                _hash(access_token))
            if row is None:
                return None
            await conn.execute("UPDATE api_device SET last_seen_at = now() WHERE id = $1", row["device_id"])
        return AuthedDevice(device_id=row["device_id"], session_id=row["session_id"],
                            role=row["role"], name=row["name"])

    # ── Device management (owner) ──────────────────────────────────────────────────────────────────────

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, platform, model, role, status, "
                "       (push_token IS NOT NULL) AS has_push, push_environment, "
                "       created_at, last_seen_at, revoked_at "
                "FROM api_device ORDER BY created_at DESC")
        return [
            {"id": str(r["id"]), "name": r["name"], "platform": r["platform"], "model": r["model"],
             "role": r["role"], "status": r["status"], "has_push": r["has_push"],
             "push_environment": r["push_environment"],
             "created_at": r["created_at"].isoformat() if r["created_at"] else None,
             "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
             "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None}
            for r in rows]

    async def revoke_device(self, device_id: UUID) -> bool:
        """Revoke a device and kill all its sessions (§24). Returns False if the device is unknown."""
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                "UPDATE api_device SET status = 'revoked', revoked_at = now() "
                "WHERE id = $1 AND status = 'active' RETURNING id", device_id)
            await conn.execute("UPDATE device_session SET revoked = true WHERE device_id = $1", device_id)
        if updated is not None:
            log.info("device_revoked", device_id=str(device_id)[:8])
        return updated is not None

    async def set_push_token(self, device_id: UUID, token: str, environment: str = "production") -> bool:
        """Register/refresh a device's APNs push token (§11). The token is opaque, not a credential."""
        if environment not in ("sandbox", "production"):
            raise ValueError("environment must be 'sandbox' or 'production'")
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                "UPDATE api_device SET push_token = $2, push_environment = $3 "
                "WHERE id = $1 AND status = 'active' RETURNING id", device_id, token, environment)
        return updated is not None

    # ── Password auth ─────────────────────────────────────────────────────────────────────────────────
    # The iPhone authenticates with the owner PASSWORD (api/password.py), which mints one of these same
    # opaque sessions. The password is the DURABLE credential: the app re-logs-in with it whenever a token
    # expires, so an expiring/revoked token can never lock the owner out.

    async def get_password_hash(self) -> str | None:
        """The stored one-way password hash, or None if the password was never set."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT auth_password_hash FROM sali.sali_state WHERE id = true")

    async def set_password(self, password_hash: str) -> None:
        """Set/rotate the login password AND revoke every live session in one transaction — rotating the
        password is the "log everyone out" switch. authenticate() requires NOT revoked, so every REST call
        401s immediately and the WS reaper tears live sockets down within ~60s."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE sali.sali_state SET auth_password_hash = $1, auth_password_set_at = now() "
                "WHERE id = true", password_hash)
            await conn.execute("UPDATE device_session SET revoked = true")
        log.info("api_password_changed")  # never logs the password or its hash

    async def login(self, *, name: str, model: str | None, platform: str) -> IssuedSession:
        """Password-verified login → a fresh owner session. Reuses ONE stable owner device row per app
        name (so repeated logins never flood the device list) and revokes that device's prior sessions so
        exactly one session stays active — the same single-active-session invariant enrollment holds."""
        clean = name.strip() or "iPhone"
        async with self._pool.acquire() as conn, conn.transaction():
            device_id = await conn.fetchval(
                "SELECT id FROM api_device WHERE name = $1 AND role = 'owner' "
                "ORDER BY created_at LIMIT 1", clean)
            if device_id is None:
                device_id = await conn.fetchval(
                    "INSERT INTO api_device (name, platform, model, role) VALUES ($1, $2, $3, 'owner') "
                    "RETURNING id", clean, platform, model)
            else:
                await conn.execute("UPDATE api_device SET status = 'active' WHERE id = $1", device_id)
            await conn.execute("UPDATE device_session SET revoked = true WHERE device_id = $1", device_id)
            session = await self._issue(conn, device_id, "owner", clean)
        log.info("password_login", device_id=str(device_id)[:8])
        return session

    async def push_targets(self) -> list[dict[str, Any]]:
        """Every ACTIVE device that has actually registered a push token (§11).

        `environment` travels with the token on purpose: a sandbox token is rejected outright by the
        production APNs host and vice versa, so the sender pushes each device on the host its own
        token came from rather than picking one globally.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, push_token, push_environment FROM api_device "
                "WHERE status = 'active' AND push_token IS NOT NULL")
        return [{"id": r["id"], "token": r["push_token"],
                 "environment": r["push_environment"] or "production"} for r in rows]

    async def clear_push_token(self, device_id: UUID) -> bool:
        """Forget a token APNs has told us is dead (app deleted, or built for the other environment).

        The DEVICE is untouched — it keeps its role, its sessions and its authority; it simply has no
        push route until the app registers again on its next launch. Revocation is a different act
        with a different method (§24).
        """
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                "UPDATE api_device SET push_token = NULL, push_environment = NULL "
                "WHERE id = $1 AND push_token IS NOT NULL RETURNING id", device_id)
        return updated is not None

    async def counts(self) -> dict[str, int]:
        async with self._pool.acquire() as conn:
            active = await conn.fetchval("SELECT count(*) FROM api_device WHERE status = 'active'")
            revoked = await conn.fetchval("SELECT count(*) FROM api_device WHERE status = 'revoked'")
            sessions = await conn.fetchval(
                "SELECT count(*) FROM device_session WHERE NOT revoked AND access_expires_at > now()")
        return {"active_devices": int(active or 0), "revoked_devices": int(revoked or 0),
                "live_sessions": int(sessions or 0)}
