"""Subprocess helper with a real timeout, resource limits, and bounded output.

- The timeout kills the whole process GROUP with SIGKILL — ``asyncio.wait_for`` only cancels
  the await, so the child would keep running and eating RAM otherwise (fix H3).
- ``limits=True`` runs the child under rlimits (address space, CPU, file size) so a jailed
  command can't OOM or peg the 15 GiB host (red-team #5).
- Output is read with a hard byte ceiling; a flooding command is killed rather than buffered
  into our own memory (red-team #6).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import signal

_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
_GiB = 1024**3
_MAX_OUTPUT = 1 << 20  # 1 MiB per stream ceiling for our buffers


class CommandTimeout(Exception):
    pass


def minimal_env() -> dict[str, str]:
    """A scrubbed environment: only the vars a tool legitimately needs, so subprocesses never
    inherit secrets from Sali's environment (the substrate for Phase-5 execute_command)."""
    return {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}


def _apply_limits() -> None:  # pragma: no cover - runs in the forked child before exec
    resource.setrlimit(resource.RLIMIT_AS, (2 * _GiB, 2 * _GiB))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 25))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _killpg(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


async def run_argv(
    argv: list[str],
    timeout: float = 8.0,
    env: dict[str, str] | None = None,
    *,
    limits: bool = False,
    max_output: int = _MAX_OUTPUT,
) -> tuple[int, str, str]:
    """Run ``argv`` (never a shell), return (returncode, stdout, stderr). Kills on timeout/flood."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # own process group → killable as a unit
        env=minimal_env() if env is None else env,
        preexec_fn=_apply_limits if limits else None,
    )
    out_buf, err_buf = bytearray(), bytearray()
    flooded = False

    async def pump(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
        nonlocal flooded
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            room = max_output - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(buf) >= max_output and not flooded:
                flooded = True
                _killpg(proc.pid)  # flood → kill the group; both streams then EOF
                return

    try:
        await asyncio.wait_for(
            asyncio.gather(pump(proc.stdout, out_buf), pump(proc.stderr, err_buf)),
            timeout=timeout,
        )
    except TimeoutError as exc:
        _killpg(proc.pid)
        await proc.wait()
        raise CommandTimeout(f"{argv[0]} timed out after {timeout}s") from exc
    except asyncio.CancelledError:
        # An OUTER cancel (e.g. the dispatch backstop firing) must not orphan the child's process
        # group — kill it before propagating, or the ssh/git/scp subprocess leaks.
        _killpg(proc.pid)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise

    await proc.wait()
    return proc.returncode or 0, out_buf.decode("utf-8", "replace"), err_buf.decode("utf-8", "replace")
