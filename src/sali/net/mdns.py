"""Sali's mDNS/Bonjour service — advertise this one Sali on the local link so the iPhone can find
it without the user typing an IP.

Discovery tells the iPhone WHERE Sali is; it does not authorize the iPhone. Every existing
authentication gate (`require_identity`, `require_controller`, bearer token, `POST /api/v1/enroll`
with a one-time pairing code) still applies. TXT records carry only public metadata — a service
tag, the stable runtime_id (a UUID that is not a secret), the software version, and the path of
the unauth `/identity` verification endpoint. NEVER credentials, tokens, or the api_token.

The service comes UP when Sali's API is up (registered from `create_app`'s lifespan) and goes DOWN
when Sali stops (unregistered on lifespan shutdown; also unregistered on process crash because
zeroconf sockets close). There is no dedicated daemon. This is a window announcement, not a Sali.

Uses `zeroconf` — pure Python, listens/announces on the standard link-local multicast group. No
avahi dependency. Reachability across the LAN depends on multicast being enabled on the router
(the default on home Wi-Fi/Ethernet).
"""

from __future__ import annotations

import contextlib
import socket
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.net.mdns")

SERVICE_TYPE = "_sali._tcp.local."
"""Bonjour service type Sali advertises. The `_sali._tcp` half is what the iPhone browses for."""


def _lan_ipv4_addresses() -> list[bytes]:
    """Best-effort enumeration of this host's non-loopback IPv4 addresses, as packed 4-byte bytes.

    Uses a socket trick that asks the kernel which local address it would use to reach 8.8.8.8 —
    that's the interface facing the default route (usually the LAN interface for a home PC).
    Falls back to hostname resolution if the trick fails, then to loopback so we still register
    something rather than crashing. We register ALL discovered addresses so a host with wired +
    Wi-Fi on the same LAN is reachable via either.
    """
    addrs: set[str] = set()
    with contextlib.suppress(Exception):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            addrs.add(s.getsockname()[0])
        finally:
            s.close()
    with contextlib.suppress(Exception):
        # getaddrinfo pulls extra interfaces (wired + Wi-Fi both live) when the router-trick found
        # only the primary. Loopback is filtered out — a client on another host can never reach it.
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    if not addrs:
        # Loopback is a degraded state — the iPhone won't reach us, but the service still registers
        # cleanly so a machine on the same host (test runner, curl) can still resolve it.
        addrs.add("127.0.0.1")
    packed = []
    for ip in sorted(addrs):
        with contextlib.suppress(Exception):
            packed.append(socket.inet_aton(ip))
    return packed


class SaliBonjour:
    """Advertise Sali as a Bonjour service on the local link, tear it down on shutdown.

    Lifecycle:
    * `await start()` registers the service. Idempotent: a second call is a no-op.
    * `await stop()` unregisters cleanly (sends goodbye packets so clients drop it immediately
      rather than waiting for the TTL to expire). Idempotent.

    Runs `zeroconf` on a background thread it manages internally; we only interact via its
    public API from asyncio using loop.run_in_executor for the blocking calls (register/unregister
    are milliseconds; not worth a full async port).
    """

    def __init__(
        self,
        *,
        runtime_id: str,
        port: int,
        version: str = "0.0.0",
        service_name: str = "Sali",
        identity_path: str = "/identity",
    ) -> None:
        self._runtime_id = runtime_id
        self._port = port
        self._version = version
        self._service_name = service_name
        self._identity_path = identity_path
        self._zc: Any = None
        self._info: Any = None
        self._registered = False

    async def start(self) -> None:
        if self._registered:
            return
        import asyncio
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError:
            log.warning("mdns_disabled_no_zeroconf",
                        hint="pip install zeroconf to enable Bonjour discovery")
            return

        loop = asyncio.get_running_loop()

        def _register() -> tuple[Any, Any]:
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            addresses = _lan_ipv4_addresses()
            # `server` — a fully-qualified name for this Sali on the .local link. Deriving it from
            # the hostname keeps it human-readable in tools like `avahi-browse`; the trailing dot
            # + .local. makes it a valid mDNS name. Duplicated instances get an "-2" suffix
            # automatically via allow_name_change=True below.
            server = f"{socket.gethostname()}.local."
            # Instance name must be unique on the link. If it's already taken (another Sali on the
            # same LAN — impossible per the single-mind rule, but a second dev machine on shared
            # Wi-Fi could clash) Zeroconf appends "-2" etc. rather than failing.
            instance = f"{self._service_name}.{SERVICE_TYPE}"
            properties = {
                # TXT record — public metadata only. Read by iPhone to VERIFY this is a Sali
                # (service=sali + a runtime_id) before opening a connection. Never a secret.
                b"service": b"sali",
                b"runtime_id": self._runtime_id.encode("ascii", errors="ignore"),
                b"version": self._version.encode("ascii", errors="ignore"),
                b"path": self._identity_path.encode("ascii", errors="ignore"),
                # A protocol version lets us evolve the discovery contract without breaking older
                # apps ("what does the iPhone side EXPECT from this TXT record"). Bump on breaks.
                b"protocol": b"1",
            }
            info = ServiceInfo(
                SERVICE_TYPE,
                instance,
                addresses=addresses or None,
                port=self._port,
                properties=properties,
                server=server,
            )
            zc.register_service(info, allow_name_change=True)
            return zc, info

        try:
            self._zc, self._info = await loop.run_in_executor(None, _register)
            self._registered = True
            log.info("mdns_registered",
                     name=self._info.name if self._info else "?",
                     port=self._port, runtime_id=self._runtime_id[:8])
        except Exception as exc:  # noqa: BLE001 — mDNS failure must never break the API
            log.warning("mdns_register_failed", error=str(exc))
            with contextlib.suppress(Exception):
                if self._zc is not None:
                    self._zc.close()
            self._zc = None
            self._info = None

    async def stop(self) -> None:
        if not self._registered:
            return
        import asyncio
        loop = asyncio.get_running_loop()

        def _unregister() -> None:
            if self._zc is None:
                return
            with contextlib.suppress(Exception):
                if self._info is not None:
                    self._zc.unregister_service(self._info)
            with contextlib.suppress(Exception):
                self._zc.close()

        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, _unregister)
        self._registered = False
        self._zc = None
        self._info = None
        log.info("mdns_unregistered")


__all__ = ["SERVICE_TYPE", "SaliBonjour"]
