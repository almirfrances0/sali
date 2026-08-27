"""Sali's local WebSocket API.

One WebSocket carries the same continuous session as the terminal. The client sends
``{"type":"message","text":...}`` and receives a stream of turn events
(``status``/``token``/``thinking``/``tool``/``final``) — the same events the terminal renders.
When Sali needs to confirm a destructive act it sends a ``confirm`` event and waits for the
client's ``{"type":"confirm_response","id":...,"ok":bool}``. Realtime typing arrives as
``{"type":"typing","text":...}``: once it lulls, Sali forms a retrieval-only hunch and pushes
back a ``sense`` event — so the UI can show it noticing while you're still typing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from sali.core.ids import new_id
from sali.runtime.session import persistent_session_id
from sali.security.redact import redact, redact_obj


class WSConfirmer:
    """Confirmation over the socket: ask the client, await its answer."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._pending: dict[str, asyncio.Future[bool]] = {}

    async def confirm(self, tool: Any, args: dict[str, object], decision: Any) -> bool:
        cid = str(new_id())
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[cid] = future
        await self._ws.send_json({
            "kind": "confirm", "text": decision.reason,
            "data": {"id": cid, "tool": tool.name, "args": redact_obj(args)},
        })
        try:
            return bool(await asyncio.wait_for(future, timeout=300))
        except (TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self._pending.pop(cid, None)

    def resolve(self, cid: str | None, ok: object) -> None:
        future = self._pending.get(cid or "")
        if future is not None and not future.done():
            future.set_result(bool(ok))


def create_app(kernel: Any) -> FastAPI:
    app = FastAPI(title="Sali", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        confirmer = WSConfirmer(websocket)
        loop = await kernel.agent_loop(confirmer=confirmer)
        session = persistent_session_id()
        work: asyncio.Queue[str | None] = asyncio.Queue()
        sensing: dict[str, asyncio.Task[None]] = {}

        async def sense_after_pause(text: str) -> None:
            # Realtime keystroke awareness: once typing lulls, form a quiet hunch (retrieval
            # only — no model, no tokens) and surface it. Debounced by cancel-on-new-keystroke.
            try:
                await asyncio.sleep(0.45)
                hunch = await loop.sense(text)
                if hunch:  # redact at the boundary (§27) — the hunch is raw memory/graph text
                    await websocket.send_json({"kind": "sense", "text": redact(hunch), "data": {}})
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a hunch must never break the socket
                pass

        async def receiver() -> None:
            try:
                while True:
                    msg = await websocket.receive_json()
                    kind = msg.get("type")
                    if kind == "confirm_response":
                        confirmer.resolve(msg.get("id"), msg.get("ok"))
                    elif kind == "message":
                        await work.put(str(msg.get("text", "")))
                    elif kind == "typing":  # Sali senses what you're typing, in realtime
                        prior = sensing.get("t")
                        if prior is not None and not prior.done():
                            prior.cancel()
                        sensing["t"] = asyncio.create_task(
                            sense_after_pause(str(msg.get("text", "")))
                        )
            except (WebSocketDisconnect, RuntimeError):
                await work.put(None)

        receive_task = asyncio.create_task(receiver())
        try:
            while True:
                text = await work.get()
                if text is None:
                    break
                if not text.strip():
                    continue
                try:
                    async for event in loop.astream(text, session_id=session):
                        await websocket.send_json(
                            {"kind": event.kind, "text": event.text, "data": event.data}
                        )
                except Exception as exc:  # noqa: BLE001 - report, keep the socket alive
                    await websocket.send_json({"kind": "error", "text": str(exc), "data": {}})
        finally:
            receive_task.cancel()
            for task in sensing.values():
                task.cancel()
            await loop.aclose()  # release the per-connection loop's resources (Sali's browser, etc.)

    return app
