"""Connectivity probe (spec §53) — privacy-minimal, low in the stack so any layer can ask "am I online?".

First a purely-local default-route check (no packets at all); only if a route exists is a bare TCP
connection opened to a public DNS resolver — a SYN and nothing more, no data sent (§64). Kept in `core`
so both the health service and the research faculty share one definition.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

_PROBE_HOSTS = (("1.1.1.1", 53), ("8.8.8.8", 53))  # public DNS resolvers — connectivity probe only


def has_default_route() -> bool:
    """True if the kernel has a default route — a purely-local check (/proc/net/route), no packets."""
    try:
        for line in Path("/proc/net/route").read_text("utf-8").splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 2 and cols[1] == "00000000":  # destination 0.0.0.0 = default route
                return True
    except OSError:
        return False
    return False


async def check_internet(*, timeout: float = 2.0) -> bool:
    """Is the internet reachable? Local route check first (no packets); a bare TCP SYN only if needed."""
    if not has_default_route():
        return False
    for host, port in _PROBE_HOSTS:
        try:
            reader_writer = asyncio.open_connection(host, port)
            _, writer = await asyncio.wait_for(reader_writer, timeout)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        except (OSError, TimeoutError):
            continue
    return False
