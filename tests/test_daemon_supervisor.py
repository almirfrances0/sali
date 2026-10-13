"""The daemon faculty supervisor: one faculty crashing must not take the whole daemon down."""

from __future__ import annotations

import asyncio

from sali.cli.main import _supervise


async def test_supervise_returns_on_clean_exit() -> None:
    stop = asyncio.Event()

    async def clean() -> None:
        return  # finished on its own (e.g. stop was set upstream)

    await asyncio.wait_for(_supervise("clean", lambda: clean(), stop), timeout=5)


async def test_supervise_isolates_a_crash_and_honors_stop() -> None:
    stop = asyncio.Event()
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        stop.set()  # set stop before crashing → the backoff wait returns at once, no restart/spin
        raise RuntimeError("boom")

    # The crash is contained: _supervise does not propagate it, and once stop is set it exits.
    await asyncio.wait_for(_supervise("flaky", lambda: flaky(), stop), timeout=5)
    assert calls["n"] == 1
