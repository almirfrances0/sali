"""Who Sali is talking to right now, and how (§17-28 of the reliability audit).

Sali kept saying "saved to /home/…" to Almir — who is on his iPhone, not at the PC. The runtime KNEW
the request came from an authenticated iPhone device over the Cloudflare tunnel, then threw that away
(`send_message`'s Identity was bound to `_`). This reconstructs it from the real edge signals — the
authenticated Identity + the HTTP request — into a small ConnectionContext the mind can read, so Sali
knows the channel (iPhone vs terminal) and whether it is local or remote, and therefore that to give
Almir a file he must SEND it, never name a local path.

Deterministic, from the transport itself — never guessed from the message text.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

# Interaction channels — how Almir is reaching Sali.
TERMINAL = "terminal"
IPHONE = "iphone"
IPAD = "ipad"
APP = "app"          # an authenticated device we can't pin to a specific Apple platform
API = "api"          # a raw API client (script)
UNKNOWN = "unknown"

LOCAL = "local"
REMOTE = "remote"


def _is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


@dataclass(slots=True, frozen=True)
class ConnectionContext:
    channel: str = UNKNOWN            # terminal | iphone | ipad | app | api | unknown
    scope: str = UNKNOWN              # local | remote | unknown
    transport: str = "unknown"        # tunnel | lan | loopback | direct | local
    device_name: str | None = None
    device_id: Any = None
    role: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.scope == REMOTE

    @property
    def on_a_phone(self) -> bool:
        return self.channel in (IPHONE, IPAD, APP)

    def describe(self) -> str:
        """One grounded line for the self-state so Sali knows where Almir is and how to reach him — in
        particular that a local file path does NOT reach a phone."""
        where = {LOCAL: "on the local network", REMOTE: "remotely, over the tunnel"}.get(self.scope, "")
        if self.channel == TERMINAL:
            return ("Almir is talking to you from the terminal on this machine"
                    + (" (local)." if self.scope == LOCAL else "."))
        if self.on_a_phone:
            dev = "iPad" if self.channel == IPAD else "iPhone"
            return (f"Almir is talking to you from his {dev} (the app), {where}. He is NOT at this "
                    "machine — a file you save to a local path will NOT reach him, so to give him a "
                    "file you must SEND it with send_file (zip a folder first).")
        if self.channel in (APP, API):
            return (f"Almir is connected through the {self.channel} {where}. To give him a file, SEND "
                    "it with send_file — a local path may not reach him.")
        return ""


def classify(identity: Any, request: Any, *, platform: str | None = None) -> ConnectionContext:
    """Build the ConnectionContext from the authenticated Identity + the HTTP request. `platform` is the
    device's declared platform (api_device.platform, e.g. 'ios'/'ipados'), looked up by the caller."""
    kind = getattr(identity, "kind", "") if identity is not None else ""
    device_id = getattr(identity, "device_id", None) if identity is not None else None
    name = getattr(identity, "name", None) if identity is not None else None
    role = getattr(identity, "role", None) if identity is not None else None

    # Transport / scope from the REAL edge, not the message.
    headers = getattr(request, "headers", {}) or {}
    cf = headers.get("cf-connecting-ip") or headers.get("cf-ray") or headers.get("cf-connecting-ipv6")
    client = getattr(request, "client", None)
    client_host = getattr(client, "host", "") if client is not None else ""
    if cf:
        scope, transport = REMOTE, "tunnel"          # arrived through the Cloudflare tunnel
    elif kind == "host":
        scope, transport = LOCAL, "local"            # the host token — this very machine
    elif client_host and _is_private(client_host):
        scope, transport = LOCAL, ("loopback" if client_host in ("127.0.0.1", "::1") else "lan")
    elif client_host:
        scope, transport = REMOTE, "direct"
    else:
        scope, transport = UNKNOWN, "unknown"

    # Channel from the identity + platform.
    if kind == "host":
        channel = TERMINAL
    elif kind == "device":
        p = (platform or "").lower()
        channel = IPAD if "ipad" in p else IPHONE if ("ios" in p or "iphone" in p) else APP
    else:
        channel = API

    return ConnectionContext(channel=channel, scope=scope, transport=transport,
                             device_name=name, device_id=device_id, role=role)


__all__ = ["ConnectionContext", "classify", "TERMINAL", "IPHONE", "IPAD", "APP", "API",
           "LOCAL", "REMOTE", "UNKNOWN"]
