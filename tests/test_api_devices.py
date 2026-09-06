"""Device enrollment, sessions, refresh rotation, and revocation (Prompt 13 §23-§24, §43).

Proves the security substrate directly against the DeviceStore (no HTTP needed):
- enrollment succeeds only with a valid one-time code; an expired or already-used code cannot be redeemed
- access tokens expire; refresh rotates and invalidates the old refresh token
- revoking a device kills its live sessions immediately
- only token HASHES are stored — never the plaintext
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.api.devices import DeviceStore, _hash, _normalize_code

pytestmark = pytest.mark.db


async def test_enroll_then_authenticate(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, expires_at = await store.mint_enrollment_code(role="owner", label="Almir's iPhone")
    session = await store.redeem_enrollment(code, name="Almir's iPhone", model="iPhone16,1")
    assert session is not None and session.role == "owner"
    # the freshly issued access token authenticates to the same device
    ident = await store.authenticate(session.access_token)
    assert ident is not None and ident.device_id == session.device_id and ident.role == "owner"


async def test_only_hashes_are_stored(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None
    async with live_pool.acquire() as c:
        # the plaintext code/token never appear in the tables — only their sha256 hashes
        assert await c.fetchval("SELECT count(*) FROM enrollment_code WHERE code_hash=$1",
                                _hash(_normalize_code(code))) == 1
        assert await c.fetchval("SELECT count(*) FROM device_session WHERE access_hash=$1",
                                _hash(session.access_token)) == 1
        # the raw secret is nowhere
        assert await c.fetchval("SELECT count(*) FROM enrollment_code WHERE code_hash=$1",
                                code) == 0


async def test_enrollment_code_is_single_use(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    first = await store.redeem_enrollment(code, name="iPhone A")
    second = await store.redeem_enrollment(code, name="iPhone B")  # same code again
    assert first is not None and second is None  # single-use enforced


async def test_expired_enrollment_code_is_rejected(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    async with live_pool.acquire() as c:
        await c.execute("UPDATE enrollment_code SET expires_at = now() - interval '1 minute'")
    assert await store.redeem_enrollment(code, name="iPhone") is None  # expired → cannot enroll


async def test_unknown_code_is_rejected(live_pool: Any) -> None:
    assert await DeviceStore(live_pool).redeem_enrollment("ZZZZZ-ZZZZZ", name="iPhone") is None


async def test_formatted_and_unicode_dashes_code_redemption(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    # Code with em-dash and spaces/lowercase should normalize and redeem successfully
    parts = code.split("-")
    formatted_code = f" {parts[0].lower()} — {parts[1].lower()} \n"
    session = await store.redeem_enrollment(formatted_code, name="iPhone Unicode Dash")
    assert session is not None and session.role == "owner"


async def test_access_token_expires(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None
    async with live_pool.acquire() as c:
        await c.execute("UPDATE device_session SET access_expires_at = now() - interval '1 second'")
    assert await store.authenticate(session.access_token) is None  # expired → not authenticated


async def test_refresh_rotates_and_invalidates_old_token(live_pool: Any) -> None:
    """Rotation issues fresh tokens and immediately invalidates the OLD ACCESS TOKEN.
    The OLD refresh token stays USABLE for a 90-second grace window (production fix — see
    tests/test_refresh_grace.py + src/sali/api/devices.py refresh_session for the rationale:
    a lost response after commit was forcing re-enrollment). Outside the grace window (aged
    row) the old refresh is dead; that's covered in test_refresh_grace_expires_after_90_seconds.
    """
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None

    rotated = await store.refresh_session(session.refresh_token)
    assert rotated is not None and rotated.access_token != session.access_token
    # new access token works
    assert await store.authenticate(rotated.access_token) is not None
    # the OLD ACCESS token is dead (its device_session was revoked at rotation time)
    assert await store.authenticate(session.access_token) is None
    # the OLD refresh token is STILL VALID within the 90-second grace window — this is the
    # production fix that eliminates re-enrollment prompts after a lost-response scenario.
    grace_reissue = await store.refresh_session(session.refresh_token)
    assert grace_reissue is not None, (
        "grace-window redelivery must accept an already-rotated refresh — Almir's "
        "'re-enrollment on restart' complaint recurs otherwise. See test_refresh_grace.py "
        "for the full contract.")
    # And the grace re-issue must retire the previous successor to preserve single-active-session.
    assert await store.authenticate(rotated.access_token) is None


async def test_revoked_device_loses_access(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None
    assert await store.authenticate(session.access_token) is not None

    assert await store.revoke_device(session.device_id) is True
    # every session bound to the device is dead immediately
    assert await store.authenticate(session.access_token) is None
    # a revoked device can't be refreshed back to life
    assert await store.refresh_session(session.refresh_token) is None


async def test_revoke_unknown_device_returns_false(live_pool: Any) -> None:
    from uuid import uuid4
    assert await DeviceStore(live_pool).revoke_device(uuid4()) is False


async def test_push_token_registration(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None
    assert await store.set_push_token(session.device_id, "apns-opaque-token", "production") is True
    devices = await store.list_devices()
    mine = next(d for d in devices if d["id"] == str(session.device_id))
    assert mine["has_push"] is True and mine["push_environment"] == "production"


async def test_counts(live_pool: Any) -> None:
    store = DeviceStore(live_pool)
    code, _ = await store.mint_enrollment_code()
    session = await store.redeem_enrollment(code, name="iPhone")
    assert session is not None
    counts = await store.counts()
    assert counts["active_devices"] == 1 and counts["live_sessions"] >= 1
    await store.revoke_device(session.device_id)
    counts2 = await store.counts()
    assert counts2["revoked_devices"] == 1 and counts2["active_devices"] == 0
