"""notify — Sali reaches out to Almir on its own (§44 notifications).

Sali isn't only reactive: when it has something worth saying — a scheduled check found something, a
long job finished, it noticed a change — it can pop a desktop notification, and optionally open a
terminal window with the full message. Works from an interactive session or the background daemon
(the systemd unit exports DISPLAY + the DBUS session bus so notifications reach the desktop).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


def _desktop_env() -> dict[str, str]:
    # A background daemon has no DISPLAY/DBUS in its env by default; fall back to the logged-in
    # user's session so notify-send / the terminal actually reach Almir's desktop.
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    return env


class Notify(Tool):
    name = "notify"
    description = (
        "Tell Almir something proactively, without waiting to be asked — pops a desktop "
        "notification with a short message. Set terminal=true to ALSO open a terminal window with "
        "the full message (use it for anything longer or important). Use this when a scheduled "
        "check, a finished job, or something you noticed is worth telling him now."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "What to tell Almir."},
            "title": {"type": "string", "description": "Short title (default 'Sali')."},
            "terminal": {"type": "boolean", "description": "Also open a terminal window with it."},
        },
        "required": ["message"],
    }
    risk_level = RiskLevel.R1  # just talking to Almir — benign
    capabilities = frozenset({Capability.EXECUTE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        message = str(args.get("message", "")).strip()
        if not message:
            return ToolResult(ok=False, display="nothing to say", error="message is required")
        title = str(args.get("title") or "Sali").strip()
        # Ground this SELF-INITIATED message before it reaches Almir's desktop (§ proactive grounding).
        # A proactive turn carries no tool receipts, so strike only the receipt-free families — a
        # capability Sali lacks ("I texted your wife"), a machine state the machine disproves — never
        # action_done/file_send, which would over-strike a legitimate "I finished X". Fail-open: a
        # grounder hiccup must never block a notification. Struck strikes feed the same /grounding ledger.
        with contextlib.suppress(Exception):
            from sali.verify.response_claims import validate_proactive
            _rv = await validate_proactive(message, cap_of={})
            if _rv.changed:
                message = _rv.rewritten
                if getattr(ctx, "pool", None) is not None:
                    from sali.runtime.grounding_log import GroundingLog
                    await GroundingLog(ctx.pool).record(session_id=None, run_id=None, claims=_rv.struck)
        env = _desktop_env()
        sent = _notify_send(title, message, env)
        opened = _open_terminal(title, message, env) if args.get("terminal") else False

        # REACH ALMIR WHERE HE ACTUALLY IS. This tool only ever fired a libnotify popup at DISPLAY=:0 —
        # on a headless box that is nobody, yet it returned "told Almir", so Sali believed he had spoken.
        # Emitting agent.message puts it in the phone's chat through the existing bridge (the event
        # trigger fires pg_notify -> EventBridge -> WebSocket -> chat + APNs); no new transport.
        delivered_to_chat = False
        with contextlib.suppress(Exception):
            if getattr(ctx, "pool", None) is not None:
                from sali.runtime.session import persistent_session_id

                sid = persistent_session_id()
                async with ctx.pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO conversation (id, channel) VALUES ($1, 'agent') "
                        "ON CONFLICT (id) DO NOTHING", sid)
                    await conn.execute(
                        "INSERT INTO message (conversation_id, seq, role, content) "
                        "SELECT $1, coalesce(max(seq), 0) + 1, 'agent', $2 "
                        "FROM message WHERE conversation_id = $1", sid, message[:2000])
                    await conn.execute(
                        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                        "VALUES ('agent.message', 'agent', $1, $2)",
                        sid, {"text": message[:2000], "importance": "update",
                              "channel": "agent_message", "session_id": str(sid)})
                delivered_to_chat = True

        if not delivered_to_chat and not sent and not opened:
            return ToolResult(ok=False, display="couldn't reach Almir",
                              error="no chat channel and no desktop session to notify on")
        how = ("chat" if delivered_to_chat else "") + (" + notification" if sent else "") \
            + (" + terminal" if opened else "")
        return ToolResult(ok=True, output={"delivered": how.strip(" +")},
                          display=f"told Almir ({how.strip(' +')})")


def _notify_send(title: str, message: str, env: dict[str, str]) -> bool:
    if shutil.which("notify-send") is None:
        return False
    try:
        subprocess.run(  # noqa: S603 - fixed argv, Sali's own machine
            ["notify-send", "--app-name=Sali", "--icon=dialog-information", title, message],
            env=env, timeout=10, check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _open_terminal(title: str, message: str, env: dict[str, str]) -> bool:
    term = _pick_terminal()
    if term is None:
        return False
    # Write the message to a temp file and show it — avoids any shell-quoting of the message text.
    fd, path = tempfile.mkstemp(prefix="sali-msg-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"── {title} ──\n\n{message}\n")
    script = f'cat {path}; echo; read -n1 -s -p "Press any key to close…"; rm -f {path}'
    argv = [term, "-e", "bash", "-lc", script]
    try:
        subprocess.Popen(  # noqa: S603 - detached terminal, Sali's own machine
            argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        return False
    return True


def _pick_terminal() -> str | None:
    for term in ("x-terminal-emulator", "konsole", "qterminal", "gnome-terminal", "xterm"):
        if shutil.which(term):
            return term
    return None


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(Notify())
