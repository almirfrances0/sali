"""HTTP-level authentication & authorization (Prompt 13 §22/§27/§28/§43).

Proves, over the real ASGI app:
- unauthenticated access to the data API is rejected (401)
- the public liveness probe needs no auth
- an authenticated device can read
- observer devices are read-only (403 on mutating routes); controllers/owners may write
- minting an enrollment code requires owner authority
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.apiutil import RuntimeStub, auth, build_app, client, enroll

pytestmark = pytest.mark.db


async def test_unauthenticated_data_api_is_rejected(live_pool: Any) -> None:
    app = build_app(live_pool)
    async with client(app) as c:
        for path in ("/api/v1/status", "/api/v1/tasks", "/api/v1/memory", "/api/v1/system"):
            r = await c.get(path)
            assert r.status_code == 401, path


async def test_healthz_is_public(live_pool: Any) -> None:
    app = build_app(live_pool)
    async with client(app) as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_authenticated_device_can_read(live_pool: Any) -> None:
    session = await enroll(live_pool, role="owner")
    app = build_app(live_pool)
    async with client(app) as c:
        r = await c.get("/api/v1/status", headers=auth(session))
        assert r.status_code == 200
        assert r.json()["model"]  # a real status body


async def test_invalid_token_is_rejected(live_pool: Any) -> None:
    app = build_app(live_pool)
    async with client(app) as c:
        r = await c.get("/api/v1/status", headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401


async def test_observer_is_read_only(live_pool: Any) -> None:
    observer = await enroll(live_pool, role="observer")
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        # observers may READ
        assert (await c.get("/api/v1/tasks", headers=auth(observer))).status_code == 200
        # …but not send messages or control anything (§27)
        r = await c.post("/api/v1/conversation/message",
                         json={"content": "do something"}, headers=auth(observer))
        assert r.status_code == 403


async def test_controller_can_send_message(live_pool: Any) -> None:
    controller = await enroll(live_pool, role="controller")
    runtime = RuntimeStub(live_pool)
    app = build_app(live_pool, runtime=runtime)
    async with client(app) as c:
        r = await c.post("/api/v1/conversation/message",
                         json={"content": "hello Sali"}, headers=auth(controller))
        assert r.status_code == 200 and r.json()["status"] == "accepted"


async def test_minting_a_code_requires_owner(live_pool: Any) -> None:
    controller = await enroll(live_pool, role="controller")
    owner = await enroll(live_pool, role="owner")
    app = build_app(live_pool)
    async with client(app) as c:
        # no token → 401
        assert (await c.post("/api/v1/enroll/code", json={"role": "observer"})).status_code == 401
        # controller → 403 (owner authority required)
        r = await c.post("/api/v1/enroll/code", json={"role": "observer"}, headers=auth(controller))
        assert r.status_code == 403
        # owner → 200, and the code is a display-formatted string
        r = await c.post("/api/v1/enroll/code", json={"role": "observer"}, headers=auth(owner))
        assert r.status_code == 200 and "-" in r.json()["code"]


async def test_enroll_over_http_then_use_the_session(live_pool: Any) -> None:
    # owner mints a code over HTTP; a fresh device redeems it and immediately uses the returned session.
    owner = await enroll(live_pool, role="owner")
    app = build_app(live_pool)
    async with client(app) as c:
        minted = (await c.post("/api/v1/enroll/code", json={"role": "controller"},
                               headers=auth(owner))).json()
        enrolled = (await c.post("/api/v1/enroll",
                                 json={"code": minted["code"], "name": "Fresh iPhone"})).json()
        assert enrolled["role"] == "controller" and enrolled["access_token"]
        # the brand-new session authenticates
        r = await c.get("/api/v1/status", headers=auth(enrolled["access_token"]))
        assert r.status_code == 200
        # and refresh returns a new pair
        refreshed = (await c.post("/api/v1/auth/refresh",
                                  json={"refresh_token": enrolled["refresh_token"]})).json()
        assert refreshed["access_token"] and refreshed["access_token"] != enrolled["access_token"]
