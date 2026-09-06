---
name: FastAPI
tags: [fastapi, python, api, async, pydantic, uvicorn]
dependencies: [python]
conflicts: []
version_hint: fastapi@0.115+
project_detect:
  - pyproject.toml
  - requirements.txt
  - main.py
  - app/main.py
summary: FastAPI is async Python + Pydantic + starlette. Routes are ordinary async functions with typed parameters. Dependency injection via `Depends`; request/response validation via Pydantic; docs auto-generated at /docs. Prefer async I/O throughout (asyncpg, httpx, aiofiles); sync work in `run_in_threadpool` when unavoidable. Auth via a shared `Depends` on the router. Errors as `HTTPException` with a stable shape. WebSocket at `@app.websocket("/ws")`. Bind uvicorn to 0.0.0.0 behind a reverse proxy; loopback-only for tunnel-only deployments.
---

# FastAPI (production)

## Structure

    app/
      main.py                # create_app() — the WINDOW; does not construct the runtime
      api/
        routes/
          users.py           # router = APIRouter(prefix="/users", ...)
          orders.py
      dependencies.py        # shared Depends() functions
      models/                # pydantic request/response models
      db.py                  # async pool factory

* `create_app()` returns the FastAPI instance; the process entry point calls it. This keeps the
  app pure so tests can build a fresh app per test.
* Router at package level; each domain gets its own file. Include with `app.include_router(...)`.
* Never construct database pools or model clients at import time — inject via `Depends`.

## Routes

    from fastapi import APIRouter, Depends, HTTPException
    router = APIRouter(prefix="/users", dependencies=[Depends(require_identity)])

    @router.get("/{user_id}", response_model=UserOut)
    async def get_user(user_id: UUID, pool = Depends(get_pool)) -> UserOut:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT ... WHERE id = $1", user_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"user {user_id} not found")
        return UserOut(**row)

* `response_model` is documentation AND enforcement — FastAPI serialises through it.
* Path parameters are typed (`user_id: UUID` → automatic validation).
* Errors are `HTTPException(status_code, detail)` — a consistent shape front-ends can rely on.
* Router-level `dependencies=` runs on every route in the router (auth is the classic use).

## Dependency injection

    async def get_pool() -> AsyncGenerator[Pool, None]:
        pool = await asyncpg.create_pool(...)
        try: yield pool
        finally: await pool.close()

    async def require_identity(request: Request) -> Identity:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        ident = await authenticate(token)
        if ident is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return ident

* `Depends()` supports async generators (yield) — the `finally:` runs after the response is sent.
* Nested deps compose: an endpoint `Depends(require_controller)` chains internally to
  `Depends(require_identity)`; each runs once per request.
* NEVER call `Depends()` inside a function body — it only works as a parameter default.

## Pydantic

    class OrderIn(BaseModel):
        product_id: UUID
        quantity: int = Field(ge=1, le=1000)
        note: str | None = Field(default=None, max_length=280)

* Prefer Pydantic v2 (fastapi >= 0.100). `Field()` for validators; `model_validator` for cross-
  field rules; `computed_field` for derived output.
* Separate `In` and `Out` models — request shape and response shape are DIFFERENT things.
* Never leak internal ids or password hashes in an `Out`; use `model_config = ConfigDict(...)`
  + explicit field lists.

## Async I/O

* Every I/O call in a route is `await` on an async client (asyncpg for Postgres, httpx.AsyncClient
  for HTTP, aiofiles for filesystem when needed, aioredis for Redis).
* A SYNC call in a `async def` route BLOCKS the event loop for every other in-flight request.
* If a library is sync-only (a legacy SDK), wrap ONE call in `fastapi.concurrency.run_in_threadpool`.
* If a WHOLE route is CPU-bound (image processing, ML inference), use `def` (not `async def`)
  and FastAPI runs it in the threadpool automatically.

## WebSockets

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token: str | None = None):
        header = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
        effective = header or token
        ident = await authenticate(effective)
        if ident is None:
            await websocket.close(code=4001, reason="unauthorized"); return
        await websocket.accept()
        # ...

* Accept AUTH via header OR `?token=` query param — some WS clients can't set headers.
* Custom close codes (4001 unauthorized, 4029 too many connections, 1000 normal) let clients
  handle failure modes distinctly.
* Backpressure: `websocket.send_text(...)` in a tight loop over a slow client will fill the OS
  buffer and block. Track per-client last-send time; drop or disconnect stragglers.

## Middleware

* CORS: `app.add_middleware(CORSMiddleware, allow_origins=[...], allow_credentials=False)`.
  Never `allow_origins=["*"], allow_credentials=True` — CORS rejects it.
* Request-size cap: custom middleware reading `content-length`; reject over-cap requests with
  413.
* Auth is a DEPENDENCY, not middleware — middleware would run BEFORE route matching, so a
  request to `/openapi.json` would try to authenticate.

## Background tasks vs jobs

* `BackgroundTasks` in the request handler runs AFTER the response but IN THE SAME PROCESS. Good
  for send-email-after-signup. Bad for anything that must survive process restart.
* For durable jobs: use a real queue (RQ, Celery, arq, or your app's own queue) and a separate
  worker process.

## Auth

* JWT: fine, but keep short-lived (15 min) + refresh flow. Never mint long-lived JWTs.
* Session/cookie: fine for browser apps behind same-origin.
* API key: fine for machine-to-machine; per-key rate limits.
* Never rely on `Origin` or `Referer` for security — spoofable.

## Testing

    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/users/123", headers={"authorization": "Bearer test"})
    assert resp.status_code == 200

* `TestClient` runs the app in-process; no live network. Fast; deterministic.
* For pool-based deps: override with `app.dependency_overrides[get_pool] = lambda: fake_pool`.
* For WS: `client.websocket_connect("/ws")` yields a context manager with `.send_text` / `.receive_text`.
* Fixture per-test app: `@pytest.fixture def app(): return create_app()` — avoids state leaks.

## Deployment

* Bind uvicorn behind a reverse proxy (Nginx, Cloudflare Tunnel, Traefik). Don't expose uvicorn
  directly to the Internet.
* Workers: `uvicorn app.main:app --workers N` — N ≈ 2*cores for I/O-bound; 1 for CPU-bound
  (add real workers via gunicorn+uvicorn workers).
* Health checks: `GET /healthz` returning `{"status":"ok"}`. Unauth. LB uses this.
* Logs: structlog / structured JSON; correlate by request id (uvicorn generates one).
* Metrics: Prometheus exporter (prometheus-fastapi-instrumentator) — endpoint at /metrics,
  protected.

## Common pitfalls

* Blocking I/O in `async def`. Detect with `asyncio.get_running_loop().slow_callback_duration`.
* `Depends` reused as a default across requests carries STATE. Use factories (`Depends(get_pool)`
  where `get_pool` returns a fresh pool from the app state), not closures over a global.
* `response_model` set on an endpoint that also does `return JSONResponse(...)` — FastAPI
  won't re-validate. Return a Pydantic model instance instead.
* WebSocket auth as a Depends: doesn't work. WS endpoints read auth manually from headers/query.
