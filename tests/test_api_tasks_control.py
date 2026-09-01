"""Task management over the API (Prompt 13 §14/§15/§34/§43).

Proves the app controls tasks through the SAME backend authority (never a UI-only button):
- pause → resume round-trips the task as the same task
- abandon = intent revocation (Prompt 12): tombstoned, live row gone, and NEVER shown as active
- an abandoned task can be intentionally revived into a new task
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.events.publisher import EventPublisher
from sali.tasks.revocation import RevocationStore
from sali.tasks.store import TaskStore
from tests.apiutil import RuntimeStub, auth, build_app, client, enroll

pytestmark = pytest.mark.db


async def _active_task(pool: Any, objective: str = "Build a website") -> Any:
    store = TaskStore(pool, EventPublisher(pool))
    task = await store.create(objective, ["scaffold", "deploy"])
    await store.activate(task.id)
    return task


async def test_pause_and_resume(live_pool: Any) -> None:
    session = await enroll(live_pool, role="controller")
    task = await _active_task(live_pool)
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r = await c.post(f"/api/v1/tasks/{task.id}/pause", headers=auth(session))
        assert r.status_code == 200 and r.json()["status"] == "paused"
        r = await c.post(f"/api/v1/tasks/{task.id}/resume", headers=auth(session))
        assert r.status_code == 200 and r.json()["status"] == "running"


async def test_abandon_revokes_and_hides_from_active(live_pool: Any) -> None:
    session = await enroll(live_pool, role="owner")
    task = await _active_task(live_pool)
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        r = await c.post(f"/api/v1/tasks/{task.id}/abandon", headers=auth(session))
        assert r.status_code == 200 and r.json()["status"] == "abandoned"

        # the live row is gone (archived + removed) — a GET 404s, and it is NOT presented as active (§15)
        assert (await c.get(f"/api/v1/tasks/{task.id}", headers=auth(session))).status_code == 404
        listing = (await c.get("/api/v1/tasks", headers=auth(session))).json()
        assert listing["active_task_id"] is None
        assert all(t["id"] != str(task.id) for t in listing["tasks"])

        # it appears in the revoked-intent tombstone list, marked historical (§15)
        revoked = (await c.get("/api/v1/intent/revoked", headers=auth(session))).json()
        assert any(r["task_id"] == str(task.id) for r in revoked["revoked"])

    # durable tombstone exists and blocks resurrection
    assert await RevocationStore(live_pool).is_revoked(task.id) is True


async def test_abandon_requires_controller(live_pool: Any) -> None:
    observer = await enroll(live_pool, role="observer")
    task = await _active_task(live_pool)
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        assert (await c.post(f"/api/v1/tasks/{task.id}/abandon",
                             headers=auth(observer))).status_code == 403


async def test_revive_creates_a_new_task(live_pool: Any) -> None:
    session = await enroll(live_pool, role="owner")
    task = await _active_task(live_pool, objective="Write the quarterly report")
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        await c.post(f"/api/v1/tasks/{task.id}/abandon", headers=auth(session))
        r = await c.post(f"/api/v1/tasks/{task.id}/revive", headers=auth(session))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "revived" and body["new_task_id"] != str(task.id)

        # the new task is real and active; it carries the revived objective
        new = (await c.get(f"/api/v1/tasks/{body['new_task_id']}", headers=auth(session))).json()
        assert new["objective"] == "Write the quarterly report"

    # the old tombstone is superseded (no longer blocks), the new task is authoritative
    assert await RevocationStore(live_pool).is_revoked(task.id) is False


async def test_revive_unknown_tombstone_404s(live_pool: Any) -> None:
    session = await enroll(live_pool, role="owner")
    task = await _active_task(live_pool)  # never abandoned → no tombstone
    app = build_app(live_pool, runtime=RuntimeStub(live_pool))
    async with client(app) as c:
        assert (await c.post(f"/api/v1/tasks/{task.id}/revive",
                             headers=auth(session))).status_code == 404
