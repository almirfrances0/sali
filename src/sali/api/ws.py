"""WebSocket handler — authenticated real-time event streaming for mobile clients (§25/§29).

Security (§25):
- Every connection is authenticated with a device access token (Authorization header, or ?token= for the
  handshake). Unauthenticated connections are rejected immediately with close code 4001.
- The token is never logged. Reconnect always re-authenticates (there is no unauthenticated path).

Recovery protocol (§29): the client tracks the highest durable `sequence` it has seen. On (re)connect it
sends {"type":"subscribe","after_seq":N}; the server replays every durable event with seq > N from the
authoritative `event` table (bounded), then continues the live stream — closing any gap opened by a
disconnect, Wi-Fi↔cellular handoff, backgrounding, or a server restart.

Protocol:
- Client → {"type":"ping"}                         Server → {"type":"pong"}
- Client → {"type":"subscribe","after_seq":N}      Server → replays events, then {"type":"subscribed"}
- Server → {"type":"connected", ...}               on accept
- Server → {"type":"event", "sequence":N, ...}     live + replayed events (identical shape)
- Server → {"type":"task_update"|"notification", ...}
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import WebSocket, WebSocketDisconnect

from sali.obs.log import get_logger

log = get_logger("sali.api.ws")

MAX_CONNECTIONS = 5  # single-user system — 5 is generous
MAX_REPLAY = 500     # bound a recovery replay so a long offline gap can't flood a reconnect (§40)


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self._connections: dict[UUID, WebSocket] = {}
        self._last_activity: dict[UUID, float] = {}
        self._last_event_seq: dict[UUID, int] = {}  # highest seq delivered to each client
        # Bind each socket to the device/session that authenticated it, so a revoked device or an expired
        # session can be torn down (Final audit §28 — a live WS must not outlive its authorization).
        self._device_of: dict[UUID, str | None] = {}
        self._session_of: dict[UUID, str | None] = {}

    async def connect(self, websocket: WebSocket, client_id: UUID,
                      *, device_id: str | None = None, session_id: str | None = None) -> bool:
        if len(self._connections) >= MAX_CONNECTIONS:
            await websocket.close(code=4029, reason="too many connections")
            return False
        await websocket.accept()
        self._connections[client_id] = websocket
        self._last_activity[client_id] = time.time()
        self._last_event_seq[client_id] = 0
        self._device_of[client_id] = device_id
        self._session_of[client_id] = session_id
        log.info("ws_connected", client_id=str(client_id)[:8], device=(device_id or "host")[:8])
        return True

    def disconnect(self, client_id: UUID) -> None:
        self._connections.pop(client_id, None)
        self._last_activity.pop(client_id, None)
        self._last_event_seq.pop(client_id, None)
        self._device_of.pop(client_id, None)
        self._session_of.pop(client_id, None)
        log.info("ws_disconnected", client_id=str(client_id)[:8])

    async def disconnect_device(self, device_id: str) -> int:
        """Close every live socket bound to a device — called on revoke so a revoked device stops receiving
        the event stream immediately (Final audit §28). Returns the number of sockets closed."""
        targets = [cid for cid, dev in self._device_of.items() if dev == device_id]
        for cid in targets:
            ws = self._connections.get(cid)
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close(code=4001, reason="device revoked")
            self.disconnect(cid)
        if targets:
            log.info("ws_device_disconnected", device=device_id[:8], closed=len(targets))
        return len(targets)

    async def reap_invalid(self, pool: Any) -> int:
        """Close sockets whose device session is no longer valid (revoked / expired / device revoked). Run
        periodically so a passive listener cannot keep an event stream alive past its authorization (§28).
        Host connections (no session) are always valid. Returns the number of sockets closed."""
        if pool is None:
            return 0
        sessioned = {cid: sid for cid, sid in self._session_of.items() if sid}
        if not sessioned:
            return 0
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT s.id FROM device_session s JOIN api_device d ON d.id = s.device_id "
                "WHERE s.id = ANY($1::uuid[]) AND NOT s.revoked AND s.access_expires_at > now() "
                "  AND d.status = 'active'",
                list(sessioned.values()))
        valid = {str(r["id"]) for r in rows}
        stale = [cid for cid, sid in sessioned.items() if sid not in valid]
        for cid in stale:
            ws = self._connections.get(cid)
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close(code=4001, reason="session expired or revoked")
            self.disconnect(cid)
        if stale:
            log.info("ws_reaped_invalid", closed=len(stale))
        return len(stale)

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a message to all connected clients, tracking the highest seq each has seen."""
        payload = json.dumps(message, default=str)
        seq = message.get("sequence")
        disconnected: list[UUID] = []
        for client_id, ws in self._connections.items():
            try:
                await ws.send_text(payload)
                self._last_activity[client_id] = time.time()
                if isinstance(seq, int) and seq > self._last_event_seq.get(client_id, 0):
                    self._last_event_seq[client_id] = seq
            except Exception:  # noqa: BLE001
                disconnected.append(client_id)
        for cid in disconnected:
            self.disconnect(cid)

    async def send_to(self, client_id: UUID, message: dict[str, Any]) -> bool:
        """Send a message to a specific client."""
        ws = self._connections.get(client_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(message, default=str))
            self._last_activity[client_id] = time.time()
            seq = message.get("sequence")
            if isinstance(seq, int) and seq > self._last_event_seq.get(client_id, 0):
                self._last_event_seq[client_id] = seq
            return True
        except Exception:  # noqa: BLE001
            self.disconnect(client_id)
            return False

    async def replay_since(self, client_id: UUID, after_seq: int, pool: Any) -> int:
        """Replay durable events with seq > after_seq to one client (§29). Returns the count sent.

        Replayed events use the SAME shape as live ones (type:"event", sequence, event_type, data), so the
        client's handling is uniform. The `event` table is the source of truth — never the NOTIFY."""
        if pool is None:
            return 0
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, id, event_type, subject_type, subject_id, payload, created_at "
                "FROM event WHERE seq > $1 ORDER BY seq LIMIT $2",
                after_seq, MAX_REPLAY)
        sent = 0
        for r in rows:
            payload = dict(r["payload"]) if r["payload"] else {}
            ok = await self.send_to(client_id, {
                "type": "event",
                "event_type": r["event_type"],
                "event_id": payload.get("event_id", str(r["id"])),
                "sequence": r["seq"],
                "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                "run_id": payload.get("run_id"),
                "task_id": payload.get("task_id"),
                "session_id": payload.get("session_id"),
                "origin": payload.get("origin", "system"),
                "subject_type": r["subject_type"],
                "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
                "data": {k: v for k, v in payload.items()
                         if k not in ("event_id", "run_id", "task_id", "session_id", "origin")},
                "replayed": True,
            })
            if not ok:
                break
            sent += 1
        if sent:
            log.info("ws_replayed", client_id=str(client_id)[:8], count=sent, after_seq=after_seq)
        return sent

    async def broadcast_event(self, event_type: str, data: dict[str, Any],
                              *, sequence: int | None = None) -> None:
        """Broadcast a structured event to all clients."""
        await self.broadcast({
            "type": "event",
            "event_type": event_type,
            "event_id": str(uuid4()),
            "sequence": sequence,
            "timestamp": time.time(),
            "data": data,
        })

    async def broadcast_task_update(self, task_id: str, data: dict[str, Any]) -> None:
        """Broadcast a task state change to all clients."""
        await self.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "event_id": str(uuid4()),
            "timestamp": time.time(),
            "data": data,
        })

    async def broadcast_notification(self, title: str, body: str,
                                     data: dict[str, Any] | None = None) -> None:
        """Broadcast a notification to all clients."""
        await self.broadcast({
            "type": "notification",
            "event_id": str(uuid4()),
            "timestamp": time.time(),
            "data": {"title": title, "body": body, **(data or {})},
        })


# Global connection manager instance
manager = ConnectionManager()


async def _session_id_for_token(pool: Any, token: str) -> str | None:
    """The device_session id backing an access token (by hash), or None for the host token. Used only to
    bind a live socket to its session so the reaper can invalidate it on expiry/revocation."""
    from sali.api.devices import _hash
    async with pool.acquire() as conn:
        sid = await conn.fetchval(
            "SELECT id FROM device_session WHERE access_hash = $1", _hash(token))
    return str(sid) if sid is not None else None


async def handle_websocket(websocket: WebSocket, token: str | None = None, pool: Any = None) -> None:
    """Handle a single authenticated WebSocket connection (§25)."""
    from sali.api.auth import authenticate, get_or_create_token, verify_token

    # Authenticate — a device access token via the pool, or the host token as local fallback. Reject
    # immediately if neither resolves. Reconnect runs through this same gate (no unauthenticated path).
    ident = await authenticate(pool, token) if pool is not None else None
    if ident is None and not verify_token(token, get_or_create_token()):
        await websocket.close(code=4001, reason="unauthorized")
        log.warning("ws_auth_failed")
        return

    # Bind the socket to the authenticating device/session so it can be torn down on revoke/expiry (§28).
    device_id = str(ident.device_id) if (ident is not None and ident.device_id is not None) else None
    session_id = None
    if pool is not None and token:
        with contextlib.suppress(Exception):
            row = await _session_id_for_token(pool, token)
            session_id = row

    client_id = uuid4()
    if not await manager.connect(websocket, client_id, device_id=device_id, session_id=session_id):
        return

    try:
        await manager.send_to(client_id, {
            "type": "connected",
            "client_id": str(client_id),
            "timestamp": time.time(),
        })

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    await manager.send_to(client_id, {"type": "pong", "timestamp": time.time()})
                elif msg_type == "subscribe":
                    # Event-sequence recovery (§29): replay everything the client missed, then go live.
                    after_seq = msg.get("after_seq")
                    replayed = 0
                    if isinstance(after_seq, int) and after_seq >= 0:
                        replayed = await manager.replay_since(client_id, after_seq, pool)
                    await manager.send_to(client_id, {
                        "type": "subscribed", "channel": msg.get("channel"), "replayed": replayed})
                else:
                    log.debug("ws_unknown_message", client_id=str(client_id)[:8], type=msg_type)
            except json.JSONDecodeError:
                log.warning("ws_invalid_json", client_id=str(client_id)[:8])

    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_error", client_id=str(client_id)[:8], error=str(exc)[:100])
        manager.disconnect(client_id)
