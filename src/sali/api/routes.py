"""HTTP snapshot endpoints + the /stream event bridge.

Every route is a thin, redacting projection of a facade the runtime already owns — the browser can load
initial state (self, world, graph, memory, tasks, events, tools, runs) and then tail the live firehose on
/stream. No business logic lives here; `safe()` redacts every payload at the boundary (§34). Reads are
open on the localhost trust model (peer-auth datastore, no added auth gate — freedom, not restricted).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from sali.api.serialize import safe, to_jsonable
from sali.api.stream import frame
from sali.core.knowledge import classify_knowledge, epistemic_status
from sali.learning import queue as learning_queue
from sali.tools.builtins.history_tool import _HISTORY_SQL, execution_record
from sali.tools.builtins.memory_audit_tool import _audit_one

if TYPE_CHECKING:
    from sali.api.services import WebServices


def _svc(request: Request) -> WebServices:
    services: WebServices = request.app.state.services
    return services


def _uuid(raw: str) -> UUID:
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="not a valid id") from None


def _hit_view(hit: Any) -> dict[str, Any]:
    """A memory hit for the inspector: the ranking signals + the DERIVED epistemic label (never stored)."""
    m = hit.memory
    kt = classify_knowledge(m.source, m.layer, needs_grounding=m.needs_grounding, confidence=m.confidence)
    return {
        "id": str(m.id), "content": m.content, "layer": to_jsonable(m.layer),
        "source": to_jsonable(m.source), "confidence": m.confidence,
        "knowledge_type": to_jsonable(kt), "epistemic_status": epistemic_status(kt, m.confidence),
        "score": hit.score, "similarity": hit.similarity,
        "effective_confidence": hit.effective_confidence, "freshness": hit.freshness_factor,
        "stale": hit.stale, "retriever": hit.retriever, "scope": m.scope,
        "valid_from": to_jsonable(m.valid_from), "last_verified": to_jsonable(m.last_verified),
        "needs_grounding": m.needs_grounding,
    }


def add_routes(app: FastAPI) -> None:  # noqa: C901 - a flat table of thin read endpoints
    # ── presence / self / health ──────────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def api_health(request: Request) -> Any:
        health = await _svc(request).health.check(probe_embedder=False)
        return {"subsystems": health.subsystems, "degraded": health.degraded,
                "all_ok": health.all_ok, "internet": health.internet, "detail": safe(health.detail)}

    @app.get("/api/presence")
    async def api_presence(request: Request) -> Any:
        return await _svc(request).presence()

    @app.get("/api/self")
    async def api_self(request: Request) -> Any:
        return safe(await _svc(request).self_state.assemble())

    @app.get("/api/world")
    async def api_world(request: Request) -> Any:
        return safe(await _svc(request).world.snapshot(with_resources=True))

    @app.get("/api/attention/counts")
    async def api_attention(request: Request, hours: int = 24) -> Any:
        return await _svc(request).attention_counts(hours=hours)

    # ── graph (the brain) ─────────────────────────────────────────────────────────────────────────
    @app.get("/api/graph/snapshot")
    async def api_graph_snapshot(request: Request, limit: int = 250) -> Any:
        return safe(await _svc(request).graph.snapshot(limit=limit))

    @app.get("/api/graph/node/{node_id}")
    async def api_graph_node(request: Request, node_id: str, depth: int = 1) -> Any:
        svc = _svc(request)
        nid = _uuid(node_id)
        node = await svc.graph.get(nid)
        if node is None:
            raise HTTPException(status_code=404, detail="no such node")
        sub = await svc.graph.subgraph(nid, depth=depth)
        return safe({"node": node, "subgraph": sub})

    @app.get("/api/graph/neighborhood/{node_id}")
    async def api_graph_neighborhood(
        request: Request, node_id: str, depth: int = 1, limit: int = 80
    ) -> Any:
        sub = await _svc(request).graph.subgraph(_uuid(node_id), depth=depth, limit=limit)
        return safe(sub)

    @app.get("/api/graph/resolve")
    async def api_graph_resolve(request: Request, name: str) -> Any:
        return safe(await _svc(request).graph.resolve(name))

    # ── memory ────────────────────────────────────────────────────────────────────────────────────
    @app.get("/api/memory/search")
    async def api_memory_search(request: Request, q: str, k: int = 8) -> Any:
        hits = await _svc(request).memory.retrieve(q, k=max(1, min(k, 50)))
        return {"query": q, "hits": safe([_hit_view(h) for h in hits])}

    @app.get("/api/memory/audit")
    async def api_memory_audit(request: Request, subject: str) -> Any:
        svc = _svc(request)
        pattern = "%" + subject.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with svc.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, layer, content, source, confidence, valid_from, last_verified, "
                "  needs_grounding, scope, claim_key, structured FROM memory "
                "WHERE valid_until IS NULL AND content ILIKE $1 "
                "ORDER BY importance DESC, confidence DESC LIMIT 5", pattern)
            knowledge = [await _audit_one(conn, dict(r)) for r in rows]
        connected = await svc.graph.resolve(subject)
        return safe({"subject": subject, "found": bool(knowledge), "knowledge": knowledge,
                     "connected": connected})

    # ── the durable event log (backfill / reconnect resume) ───────────────────────────────────────
    @app.get("/api/events")
    async def api_events(
        request: Request, since: int = 0, limit: int = 200, type: str | None = None
    ) -> Any:
        limit = max(1, min(limit, 1000))
        svc = _svc(request)
        clause = "seq > $1" + (" AND event_type = $3" if type else "")
        params: list[Any] = [since, limit] + ([type] if type else [])
        async with svc.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                f"FROM event WHERE {clause} ORDER BY seq LIMIT $2", *params)
        return {"events": [frame(r) for r in rows], "since": since}

    # ── tasks / schedules / learning ──────────────────────────────────────────────────────────────
    @app.get("/api/tasks")
    async def api_tasks(request: Request, limit: int = 10) -> Any:
        tasks = await _svc(request).tasks.open_tasks(limit=max(1, min(limit, 50)))
        return safe({"open": tasks, "current": tasks[0] if tasks else None})

    @app.get("/api/schedules")
    async def api_schedules(request: Request) -> Any:
        return safe({"schedules": await _svc(request).schedules.list_all()})

    @app.post("/api/schedules")
    async def api_create_schedule(request: Request, body: dict[str, Any] = Body(...)) -> Any:  # noqa: B008
        from sali.scheduler.cron import ScheduleError

        name = str(body.get("name", "")).strip()
        when = str(body.get("when", "")).strip()
        prompt = str(body.get("prompt", "")).strip()
        if not (name and when and prompt):
            raise HTTPException(status_code=400, detail="name, when, and prompt are required")
        try:
            sched = await _svc(request).schedules.create(name, when, prompt)
        except ScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return safe({"schedule": sched})

    @app.delete("/api/schedules/{name}")
    async def api_delete_schedule(request: Request, name: str) -> Any:
        deleted = await _svc(request).schedules.delete(name)
        return {"deleted": deleted}

    @app.get("/api/learning/queue")
    async def api_learning_queue(request: Request) -> Any:
        svc = _svc(request)
        async with svc.pool.acquire() as conn:
            pending = await learning_queue.pending(conn, limit=25)
            count = await learning_queue.count_pending(conn)
        return safe({"pending": pending, "count": count})

    @app.get("/api/learning/procedures")
    async def api_learning_procedures(request: Request) -> Any:
        from sali.learning.service import LearningService

        svc = _svc(request)
        procs = await LearningService(svc.pool, svc.provider).procedures()
        return safe({"procedures": procs})

    # ── tools (catalog + real execution history) ──────────────────────────────────────────────────
    @app.get("/api/tools")
    async def api_tools(request: Request) -> Any:
        catalog = [{"name": t.name, "description": t.description, "risk": int(t.risk_level),
                    "capabilities": sorted(c.value for c in t.capabilities),
                    "idempotent": t.idempotent}
                   for t in _svc(request).registry.tools()]
        return safe({"tools": sorted(catalog, key=lambda c: c["name"])})

    @app.get("/api/tools/executions")
    async def api_tool_executions(
        request: Request, limit: int = 25, run_id: str | None = None
    ) -> Any:
        svc = _svc(request)
        lim = max(1, min(limit, 100))
        where = "WHERE te.run_id = $2" if run_id else ""
        params: list[Any] = [lim] + ([_uuid(run_id)] if run_id else [])
        async with svc.pool.acquire() as conn:
            rows = await conn.fetch(_HISTORY_SQL.format(where=where), *params)
        return safe({"executions": [execution_record(dict(r)) for r in rows]})

    # ── runs (the reasoning timeline) ─────────────────────────────────────────────────────────────
    @app.get("/api/runs")
    async def api_runs(request: Request, limit: int = 20) -> Any:
        svc = _svc(request)
        async with svc.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT run_id, session_id, user_input, state, status, iteration, started_at, "
                "  updated_at FROM agent_runs ORDER BY started_at DESC LIMIT $1",
                max(1, min(limit, 100)))
        return safe({"runs": [dict(r) for r in rows]})

    @app.get("/api/runs/{run_id}/events")
    async def api_run_events(request: Request, run_id: str) -> Any:
        svc = _svc(request)
        rid = _uuid(run_id)
        async with svc.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, kind, payload, latency_ms, tokens_in, tokens_out, created_at "
                "FROM run_events WHERE run_id=$1 ORDER BY seq", rid)
        return safe({"run_id": str(rid), "events": [dict(r) for r in rows]})


def add_stream_route(app: FastAPI) -> None:
    @app.websocket("/stream")
    async def stream(websocket: WebSocket) -> None:
        """The live firehose: every durable event, redacted, fanned out. Optional ?since=<seq> replays
        what a reconnecting client missed before tailing live."""
        await websocket.accept()
        hub = _svc_from_ws(websocket).hub
        q = hub.register()
        try:
            since = websocket.query_params.get("since")
            if since is not None:
                with contextlib.suppress(ValueError):
                    for f in await hub.backfill(int(since)):
                        await websocket.send_json(f)

            async def _forward() -> None:
                while True:
                    await websocket.send_json(await q.get())

            async def _drain() -> None:
                while True:
                    await websocket.receive()  # raises WebSocketDisconnect on close

            tasks = [asyncio.create_task(_forward()), asyncio.create_task(_drain())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.gather(*tasks, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(q)


def _svc_from_ws(websocket: WebSocket) -> WebServices:
    services: WebServices = websocket.app.state.services
    return services
