"""Remote execution over the system ssh — reuses Almir's ~/.ssh (keys, config aliases, agent).

No new dependency, no stored passwords: SSH auth is delegated wholesale to ssh-agent / ~/.ssh (which
is already in fs_deny, so Sali's own file tools can't read the keys). Transport safety is STRUCTURAL,
not friction: BatchMode=yes never hangs on a password prompt, and StrictHostKeyChecking=accept-new is
trust-on-first-use but hard-aborts (rc 255) if a known host key CHANGED — the MITM defence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sali.tools.exec import CommandTimeout, run_argv

# ssh's own failure (connection refused, auth denied, CHANGED host key) is rc 255 — distinct from the
# remote command's exit code, which may be non-zero for ordinary reasons (grep found nothing, etc.).
_SSH_FAIL = 255


def _ssh_opts(connect_timeout: int) -> list[str]:
    return ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={connect_timeout}"]


@dataclass(slots=True)
class RemoteResult:
    host: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode != _SSH_FAIL  # connected & ran; the remote rc is data, not a failure


class RemoteRunner(Protocol):
    async def run(self, host: str, command: str, *, timeout: float = 60.0) -> RemoteResult: ...
    async def put(self, host: str, local: str, remote: str, *, timeout: float = 120.0) -> RemoteResult: ...


class SshRunner:
    """Drives /usr/bin/ssh and /usr/bin/scp; inherits Sali's env so SSH_AUTH_SOCK/HOME reach ssh."""

    def __init__(self, connect_timeout: int = 10) -> None:
        self._connect_timeout = connect_timeout

    async def run(self, host: str, command: str, *, timeout: float = 60.0) -> RemoteResult:
        argv = ["ssh", *_ssh_opts(self._connect_timeout), host, command]
        return await self._exec(host, argv, timeout)

    async def put(self, host: str, local: str, remote: str, *, timeout: float = 120.0) -> RemoteResult:
        argv = ["scp", *_ssh_opts(self._connect_timeout), local, f"{host}:{remote}"]
        return await self._exec(host, argv, timeout)

    async def _exec(self, host: str, argv: list[str], timeout: float) -> RemoteResult:
        try:
            rc, out, err = await run_argv(argv, timeout=timeout)  # env=None → inherit (agent/HOME)
        except CommandTimeout:
            return RemoteResult(host, _SSH_FAIL, "", f"{argv[0]} timed out after {timeout:.0f}s")
        except FileNotFoundError as exc:
            return RemoteResult(host, _SSH_FAIL, "", str(exc))
        return RemoteResult(host, rc, out, err)


class FakeRemoteRunner:
    """In-memory ssh for CI — seeded (host, command-substring) → RemoteResult, never opens a socket."""

    def __init__(self, responses: dict[tuple[str, str], RemoteResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    async def run(self, host: str, command: str, *, timeout: float = 60.0) -> RemoteResult:
        self.calls.append((host, command))
        for (h, sub), res in self._responses.items():
            if h == host and sub in command:
                return res
        return RemoteResult(host, 0, f"[fake:{host}] {command}", "")

    async def put(self, host: str, local: str, remote: str, *, timeout: float = 120.0) -> RemoteResult:
        self.calls.append((host, f"put {local} -> {remote}"))
        return RemoteResult(host, 0, "", "")


def build_remote_runner(ssh_settings: Any) -> RemoteRunner:
    """Pick the runner from settings — the single edge that names a concrete backend (fake in CI)."""
    if getattr(ssh_settings, "backend", "ssh") == "fake":
        return FakeRemoteRunner()
    return SshRunner(connect_timeout=getattr(ssh_settings, "connect_timeout", 10))
