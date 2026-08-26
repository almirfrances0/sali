"""Subprocess helper with a timeout that actually kills the process group.

``asyncio.wait_for`` only cancels the *await* — the child (and its children) keep running
and keep eating RAM, which is fatal on a 15 GiB box (fix H3). Running in a new session and
sending SIGKILL to the whole process group on timeout is the fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")


class CommandTimeout(Exception):
    pass


def minimal_env() -> dict[str, str]:
    """A scrubbed environment: only the vars a tool legitimately needs, so subprocesses never
    inherit secrets from Sali's environment (the substrate for Phase-5 execute_command)."""
    return {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}


async def run_argv(
    argv: list[str], timeout: float = 8.0, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run ``argv`` (never a shell), return (returncode, stdout, stderr). Kills on timeout.

    The child gets a scrubbed environment by default (pass ``env`` to override)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # own process group → killable as a unit
        env=minimal_env() if env is None else env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        await proc.wait()
        raise CommandTimeout(f"{argv[0]} timed out after {timeout}s") from exc
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
