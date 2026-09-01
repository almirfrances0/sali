"""Shared helpers for the iPhone control-center API tests (Prompt 13).

The app is driven in-process with httpx's ASGITransport so it runs on the SAME event loop as `live_pool`
(asyncpg connections are loop-bound). The lifespan is a no-op for kernel=None, so we pass a stub kernel via
app.state and never start the real runtime/bridge — the security, enrollment, file, and task-control routes
need only a pool (+ an optional runtime stub for message routes)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI

from sali.api.app import create_app
from sali.api.devices import DeviceStore, IssuedSession
from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.provider.fake import FakeModelProvider


class StubKernel:
    """Minimal kernel surface the routes touch: pool(), provider, settings, close()."""

    def __init__(self, pool: Any, runtime: Any = None) -> None:
        self._pool = pool
        self.provider = FakeModelProvider()
        self.settings = Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))
        self._runtime = runtime

    async def pool(self) -> Any:
        return self._pool

    async def close(self) -> None:  # pragma: no cover - never called in tests
        pass


class RuntimeStub:
    """A stand-in for AgentRuntime for message-route tests: records messages, never spawns a model call.

    Mirrors just the surface routes/api.py touches: handle_message, cancel_foreground, is_busy, snapshot,
    and a canonical _publisher so events land in the durable log (and thus WS replay)."""

    def __init__(self, pool: Any) -> None:
        from sali.events.publisher import EventPublisher
        self.messages: list[str] = []
        self.is_busy = False
        self._publisher = EventPublisher(pool)

    async def handle_message(self, content: str, origin: str = "api") -> dict[str, Any]:
        self.messages.append(content)
        return {"status": "completed", "category": "chat", "message_id": None,
                "result": {"run_id": None}}

    async def cancel_foreground(self) -> bool:
        return False

    async def snapshot(self) -> dict[str, Any]:
        return {"task_id": None, "next_action": "", "capabilities": {}}


def build_app(pool: Any, *, runtime: Any = None) -> FastAPI:
    """A FastAPI app wired to the test pool. kernel=None → lifespan no-ops; state is set explicitly."""
    kernel = StubKernel(pool, runtime=runtime)
    app = create_app(kernel=None)
    app.state.kernel = kernel
    app.state.pool = pool
    app.state.runtime = runtime
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    """An in-process async HTTP client for the app (shares the current event loop with live_pool)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sali.test")


def auth(session: IssuedSession | str) -> dict[str, str]:
    """Bearer header for an issued session (or a raw token)."""
    token = session if isinstance(session, str) else session.access_token
    return {"Authorization": f"Bearer {token}"}


class FakeWebSocket:
    """A stand-in WebSocket for handle_websocket: records server sends, feeds scripted client messages,
    then raises WebSocketDisconnect. Runs on the test's own event loop (httpx ASGI can't do WebSockets)."""

    def __init__(self, headers: dict[str, str] | None = None,
                 incoming: list[dict[str, Any]] | None = None) -> None:
        self.headers = headers or {}
        self.query_params: dict[str, str] = {}
        self._incoming = list(incoming or [])
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_code = code

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        if self._incoming:
            return json.dumps(self._incoming.pop(0))
        from fastapi import WebSocketDisconnect
        raise WebSocketDisconnect()

    def events(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == "event"]


async def run_ws(ws: FakeWebSocket, token: str | None, pool: Any) -> FakeWebSocket:
    """Drive the real WS handler with a FakeWebSocket (cast once here; production keeps the concrete type)."""
    from typing import cast

    from starlette.websockets import WebSocket

    from sali.api.ws import handle_websocket
    await handle_websocket(cast("WebSocket", ws), token, pool)
    return ws


async def enroll(pool: Any, *, role: str = "owner", name: str = "Test iPhone") -> IssuedSession:
    """Enroll a device the direct (host) way and return its first session. Mirrors `sali enroll-code` +
    the app's redemption, without going over HTTP — the fast path for arranging test authority."""
    store = DeviceStore(pool)
    code, _ = await store.mint_enrollment_code(role=role, label=name)
    session = await store.redeem_enrollment(code, name=name, model="iPhone16,1")
    assert session is not None
    return session
