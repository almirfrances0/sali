"""Sali API server — FastAPI application.

Exposes Sali's state and capabilities over HTTP + WebSocket for mobile clients.
The terminal and iOS app share the same Sali instance, session, and state.

All message execution goes through the AgentRuntime, which ensures only ONE
AgentLoop and ONE foreground execution stream exist per process.

Usage:
    sali serve              # start the API server
    sali serve --port 8080  # custom port
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from sali.api.auth import get_or_create_token
from sali.api.routes.api import router
from sali.api.routes.enroll import router as enroll_router
from sali.api.ws import handle_websocket, manager
from sali.obs.log import get_logger

log = get_logger("sali.api")


def create_app(kernel: Any = None) -> FastAPI:
    """Create the FastAPI application with the shared runtime.

    Startup/shutdown is a lifespan context manager (the modern replacement for @app.on_event): on start it
    wires the single AgentRuntime + the cross-process EventBridge; on stop it cancels any foreground run and
    tears them down. With kernel=None (tests) it no-ops, so the app can be driven against a stub kernel."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import time
        app.state.start_time = time.time()
        get_or_create_token()
        log.info("api_server_starting")
        bridge = None
        if kernel:
            from sali.security.confirm import AutoAllowConfirmer

            # The shared runtime — this creates the single AgentLoop and coordinator for this process.
            runtime = await kernel.runtime(confirmer=AutoAllowConfirmer(), ws_broadcaster=manager)
            app.state.runtime = runtime
            pool = await kernel.pool()
            app.state.pool = pool

            # Cross-process event bridge: PostgreSQL NOTIFY → EventBus → durable event → WebSocket clients.
            from sali.events.bridge import EventBridge
            bridge = EventBridge(pool, runtime._publisher)
            runtime._publisher._bridge = bridge  # wire dedup reference
            await bridge.start()
            app.state.event_bridge = bridge
            log.info("api_runtime_initialized", session_id=str(runtime.session_id)[:8],
                     model=kernel.settings.model.chat_model)

        # Periodic WebSocket reaper: close sockets whose device session was revoked or has expired, so a
        # passive listener cannot keep the event stream alive past its authorization (Final audit §28).
        reaper: Any = None
        if kernel:
            import asyncio

            async def _reap_loop() -> None:
                while True:
                    await asyncio.sleep(60)
                    with contextlib.suppress(Exception):
                        await manager.reap_invalid(await kernel.pool())

            reaper = asyncio.create_task(_reap_loop())
        try:
            yield
        finally:
            log.info("api_server_stopping")
            if reaper is not None:
                reaper.cancel()
                with contextlib.suppress(Exception):
                    await reaper
            if bridge is not None:
                await bridge.stop()
            runtime = getattr(app.state, "runtime", None)
            if runtime is not None and getattr(runtime, "is_busy", False):
                await runtime.cancel_foreground()
            if kernel:
                await kernel.close()

    app = FastAPI(
        title="Sali API",
        description="Sali — local-first personal AI agent. One Sali, many interfaces.",
        version="0.0.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # CORS — the client is a native iOS app (no browser Origin) plus local dev; auth is a bearer token in
    # the Authorization header, never a cookie. So credentials mode is OFF: `*` origins with
    # allow_credentials=True is a contradictory/invalid CORS posture (Final audit §28). With credentials
    # off, a cross-origin site cannot ride an ambient credential, and the bearer token remains the control.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request size limit — small JSON bodies are capped tightly; file uploads (multipart to /files) get the
    # larger, still-bounded upload ceiling (§33/§40). Real enforcement of the upload size is in the handler.
    from starlette.middleware.base import BaseHTTPMiddleware

    from sali.api.files import MAX_UPLOAD_BYTES

    class RequestSizeLimit(BaseHTTPMiddleware):
        JSON_MAX = 1_048_576  # 1MB for ordinary API calls

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            if request.method in ("POST", "PUT", "PATCH"):
                cap = MAX_UPLOAD_BYTES if request.url.path.endswith("/files") else self.JSON_MAX
                content_length = request.headers.get("content-length")
                if content_length and int(content_length) > cap:
                    from starlette.responses import JSONResponse
                    return JSONResponse({"error": "request too large"}, status_code=413)
            return await call_next(request)

    app.add_middleware(RequestSizeLimit)

    # Store kernel on app state — runtime is created at startup
    app.state.kernel = kernel
    app.state.runtime = None
    app.state.start_time = None

    # REST routes — enrollment/refresh (unauthenticated, carry their own one-time secret) + the data
    # router (uniformly behind require_identity).
    app.include_router(enroll_router)
    app.include_router(router)

    # Public liveness probe — no data, no auth. For Cloudflare Tunnel / uptime checks (§26/§30).
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # WebSocket endpoint — authenticated with a device access token (Authorization header or ?token=).
    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: str | None = None,
    ) -> None:
        header_token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
        effective_token = header_token or token
        pool = getattr(app.state, "pool", None)
        if pool is None and kernel is not None:
            pool = await kernel.pool()
        await handle_websocket(websocket, effective_token, pool)

    return app
