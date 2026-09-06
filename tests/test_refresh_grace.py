"""Refresh-token grace window — the production fix for Almir's #1 complaint:
'the app requires enrollment again after restart'.

Root cause: refresh_session used to atomically revoke the old row + issue a new one. If the
client's response was lost in transit (Cloudflare restart, LTE handoff, WS reaper closing the
socket mid-response), the client was left holding the OLD refresh token; the next attempt hit
an already-revoked row and got 401; the iOS client wiped credentials and forced re-enrollment.

Fix: refresh_session now accepts a rotated refresh token within a 90-second grace window and
re-issues fresh tokens, retiring every other session on the device so the presumed lost-
response successor is invalidated and only ONE clean session remains.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_grace_window_reissues_after_lost_response(live_pool):
    """Simulate the exact failure: rotate once, then retry with the OLD refresh — must succeed
    (grace path) and produce a fresh working session."""
    from sali.api.devices import DeviceStore

    store = DeviceStore(live_pool)
    # Fresh device seeded via the same helper enroll uses.
    code, _exp = await store.mint_enrollment_code(role="controller", label="test-grace")
    session1 = await store.redeem_enrollment(
        code=code, name="test-device", platform="ios", model="test")
    assert session1 is not None
    r1 = session1.refresh_token

    # First rotation succeeds normally (this is the "response received" path).
    session2 = await store.refresh_session(r1)
    assert session2 is not None
    assert session2.refresh_token != r1  # a genuinely new refresh
    r2 = session2.refresh_token

    # Now the failure mode: the CLIENT never got session2's response. They retry with r1.
    # The OLD implementation would return None (401). The FIX returns a fresh session from the
    # grace-window path.
    session3 = await store.refresh_session(r1)
    assert session3 is not None, (
        "grace-window redelivery failed — Almir's 'requires re-enrollment on restart' complaint "
        "recurs. Expected refresh_session to accept an already-rotated refresh token within the "
        "90-second window.")
    assert session3.refresh_token != r1
    assert session3.refresh_token != r2  # genuinely fresh, not a redelivery of session2

    # And the previous successor (session2) is invalidated so single-active-session holds.
    revived = await store.refresh_session(r2)
    assert revived is None, (
        "grace-window re-issue must retire ALL other sessions on this device, otherwise a mid-air "
        "captured token would race a legitimate rotation.")


@pytest.mark.asyncio
async def test_grace_window_expires_after_90_seconds(live_pool):
    """A refresh token whose rotation is more than 90 seconds old must NOT be accepted — the
    grace window is bounded, not infinite. Tested by aging the row directly since asyncio.sleep
    would make the test slow."""
    from sali.api.devices import DeviceStore

    store = DeviceStore(live_pool)
    code, _exp = await store.mint_enrollment_code(role="controller", label="test-expiry")
    session1 = await store.redeem_enrollment(
        code=code, name="test-device", platform="ios", model="test")
    assert session1 is not None
    r1 = session1.refresh_token
    session2 = await store.refresh_session(r1)  # normal rotation
    assert session2 is not None

    # Age the rotated_at of the original row back by 120 seconds — outside the grace window.
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE device_session SET rotated_at = now() - INTERVAL '120 seconds' "
            "WHERE refresh_hash = encode(sha256($1), 'hex') AND revoked = true",
            r1.encode())

    # Grace path must reject — token is too old.
    revived = await store.refresh_session(r1)
    assert revived is None, (
        "grace window must expire; a refresh token whose rotation is 2 minutes old must not "
        "re-issue.")


@pytest.mark.asyncio
async def test_normal_rotation_unchanged_by_grace_fix(live_pool):
    """Sanity: the normal (response-received) rotation path still works exactly as before —
    old refresh → new refresh, old row revoked immediately."""
    from sali.api.devices import DeviceStore

    store = DeviceStore(live_pool)
    code, _exp = await store.mint_enrollment_code(role="controller", label="test-normal")
    s1 = await store.redeem_enrollment(
        code=code, name="test-device", platform="ios", model="test")
    assert s1 is not None
    s2 = await store.refresh_session(s1.refresh_token)
    assert s2 is not None
    assert s2.refresh_token != s1.refresh_token
    assert s2.access_token != s1.access_token
