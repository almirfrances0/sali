"""Authorized file transfer + path safety (Prompt 13 §9/§33/§43).

Unit-level: the pure helpers reject traversal and workspace escape.
HTTP-level: downloads require auth and stay inside the task workspace; metadata never leaks the host path;
uploads require a controller and are sanitized + bounded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sali.api.files import guess_content_type, is_within, safe_filename
from sali.events.publisher import EventPublisher
from sali.tasks.store import TaskStore
from tests.apiutil import auth, build_app, client, enroll

pytestmark = pytest.mark.db


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────────

def test_safe_filename_strips_traversal() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("report.pdf") == "report.pdf"
    assert safe_filename("weird name!!.txt") == "weird_name_.txt"  # unsafe runs collapse to one _
    assert safe_filename("/abs/path/to/x.zip") == "x.zip"
    assert safe_filename("...") is None
    assert safe_filename("") is None


def test_is_within_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    inside = root / "sub" / "f.txt"
    inside.parent.mkdir()
    inside.write_text("x")
    assert is_within(inside, root) is True
    assert is_within(root, root) is True
    assert is_within(tmp_path / "outside.txt", root) is False
    assert is_within(Path("/etc/hostname"), root) is False


def test_guess_content_type() -> None:
    assert guess_content_type(Path("a.json")) == "application/json"
    assert guess_content_type(Path("a.unknownext")) == "application/octet-stream"


# ── HTTP download / upload ─────────────────────────────────────────────────────────────────────────────

async def _task_with_workspace(pool: Any, tmp_path: Path) -> Any:
    store = TaskStore(pool, EventPublisher(pool))
    task = await store.create("Build a report", ["write"])
    await store.activate(task.id)
    await store.bind_workspace(task.id, objective="Build a report", explicit=None,
                               sali_works_root=str(tmp_path))
    return await store.get(task.id)


async def test_download_artifact_inside_workspace(live_pool: Any, tmp_path: Path) -> None:
    session = await enroll(live_pool, role="owner")
    task = await _task_with_workspace(live_pool, tmp_path)
    ws = Path(task.workspace_root)
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "report.txt"
    f.write_text("the report body")
    await TaskStore(live_pool).record_artifact(task.id, str(f), "created")

    app = build_app(live_pool)
    async with client(app) as c:
        meta = (await c.get(f"/api/v1/tasks/{task.id}/artifacts", headers=auth(session))).json()
        assert len(meta) == 1
        art = meta[0]
        # metadata exposes filename/size/type + a download URL — NEVER the host path (§43)
        assert art["filename"] == "report.txt" and art["size"] == len("the report body")
        assert "artifact_path" not in art and str(ws) not in str(art)
        # the download works and returns the bytes
        r = await c.get(art["download_url"], headers=auth(session))
        assert r.status_code == 200 and r.text == "the report body"


async def test_download_requires_auth(live_pool: Any, tmp_path: Path) -> None:
    task = await _task_with_workspace(live_pool, tmp_path)
    app = build_app(live_pool)
    async with client(app) as c:
        r = await c.get(f"/api/v1/tasks/{task.id}/artifacts")
        assert r.status_code == 401


async def test_artifact_outside_workspace_is_refused(live_pool: Any, tmp_path: Path) -> None:
    session = await enroll(live_pool, role="owner")
    task = await _task_with_workspace(live_pool, tmp_path)
    # an artifact recorded OUTSIDE the task workspace (stale row / tampering) must not be served
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("do not serve")
    await TaskStore(live_pool).record_artifact(task.id, str(outside), "created")

    app = build_app(live_pool)
    async with client(app) as c:
        meta = (await c.get(f"/api/v1/tasks/{task.id}/artifacts", headers=auth(session))).json()
        r = await c.get(meta[0]["download_url"], headers=auth(session))
        assert r.status_code == 403  # workspace authority enforced


async def test_upload_stores_into_workspace_and_records_artifact(live_pool: Any, tmp_path: Path) -> None:
    controller = await enroll(live_pool, role="controller")
    task = await _task_with_workspace(live_pool, tmp_path)
    app = build_app(live_pool)
    async with client(app) as c:
        r = await c.post(f"/api/v1/tasks/{task.id}/files",
                         params={"filename": "../evil name.txt"}, content=b"payload",
                         headers=auth(controller))
        assert r.status_code == 200
        body = r.json()
        assert body["filename"] == "evil_name.txt" and body["size"] == len(b"payload")  # sanitized
        # landed under <workspace>/uploads and was recorded as an artifact
        stored = Path(task.workspace_root) / "uploads" / "evil_name.txt"
        assert stored.is_file() and stored.read_bytes() == b"payload"
        arts = await TaskStore(live_pool).artifacts(task.id)
        assert any(a["tool_name"] == "upload" for a in arts)


async def test_upload_requires_controller(live_pool: Any, tmp_path: Path) -> None:
    observer = await enroll(live_pool, role="observer")
    task = await _task_with_workspace(live_pool, tmp_path)
    app = build_app(live_pool)
    async with client(app) as c:
        # no auth → 401
        r = await c.post(f"/api/v1/tasks/{task.id}/files",
                         params={"filename": "x.txt"}, content=b"x")
        assert r.status_code == 401
        # observer → 403
        r = await c.post(f"/api/v1/tasks/{task.id}/files",
                         params={"filename": "x.txt"}, content=b"x", headers=auth(observer))
        assert r.status_code == 403
