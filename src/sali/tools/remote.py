"""Remote execution over the system ssh — reuses Almir's ~/.ssh (keys, config aliases, agent).

Key auth is delegated wholesale to ssh-agent / ~/.ssh (already in fs_deny, so Sali's own file tools
can't read the keys); BatchMode=yes never hangs on a prompt, and StrictHostKeyChecking=accept-new is
trust-on-first-use but hard-aborts (rc 255) if a known host key CHANGED — the MITM defence.

Password hosts (a VPS with no key set up) work too: the password comes ONLY from the encrypted vault
(sali3 §27), fed to `sshpass -e` via the SSHPASS env var (never on the command line, never logged). A
password passed inline once is persisted to the vault so the next login just works.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from sali.tools.exec import CommandTimeout, minimal_env, run_argv


def _ssh_env() -> dict[str, str]:
    # The scrubbed env PLUS the agent socket, so ssh-agent keys work (minimal_env drops it otherwise).
    env = dict(minimal_env())
    sock = os.environ.get("SSH_AUTH_SOCK")
    if sock:
        env["SSH_AUTH_SOCK"] = sock
    return env

# ssh's own failure (connection refused, auth denied, CHANGED host key) is rc 255 — distinct from the
# remote command's exit code, which may be non-zero for ordinary reasons (grep found nothing, etc.).
_SSH_FAIL = 255


def password_ref(host: str) -> str:
    """The vault ref that holds a host's ssh password. Keyed by the HOSTNAME (after any user@), so it's
    stable across usernames: 'almir@74.207.227.95' and 'root@74.207.227.95' share 'ssh.74_207_227_95…'."""
    hostname = host.rsplit("@", 1)[-1].split()[0] if host else ""
    slug = re.sub(r"[^a-z0-9]+", "_", hostname.lower()).strip("_") or "host"
    return f"ssh.{slug}.password"


class SecretResolver(Protocol):
    """The slice of SecretStore the runner needs — resolve/persist a host password (from the vault)."""

    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, value: str) -> None: ...


def _ssh_opts(connect_timeout: int) -> list[str]:
    return ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={connect_timeout}"]


def _pw_opts(connect_timeout: int) -> list[str]:
    # Password auth: no BatchMode (sshpass supplies the password), keys off so it can't fall through
    # to a key prompt, but the CHANGED-host-key abort is still enforced.
    return ["-o", "StrictHostKeyChecking=accept-new", "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive"]


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
    async def run(self, host: str, command: str, *, timeout: float = 60.0,
                  password: str | None = None) -> RemoteResult: ...
    async def put(self, host: str, local: str, remote: str, *, timeout: float = 120.0,
                  password: str | None = None) -> RemoteResult: ...


class SshRunner:
    """Drives /usr/bin/ssh and /usr/bin/scp. Key auth by default (ssh-agent + ~/.ssh); for password
    hosts it resolves the password from the encrypted vault (or persists one that just authenticated)
    and feeds `sshpass -e`. The single-operation cap comes from settings (`ssh.command_timeout`)."""

    def __init__(self, connect_timeout: int = 10, command_timeout: float = 120.0,
                 secrets: SecretResolver | None = None) -> None:
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._secrets = secrets

    async def run(self, host: str, command: str, *, timeout: float | None = None,
                  password: str | None = None) -> RemoteResult:
        pw = self._resolve(host, password)
        if pw is not None:
            argv = ["sshpass", "-e", "ssh", *_pw_opts(self._connect_timeout), host, command]
            res = await self._exec(host, argv, timeout, env={**_ssh_env(), "SSHPASS": pw})
        else:
            argv = ["ssh", *_ssh_opts(self._connect_timeout), host, command]
            res = await self._exec(host, argv, timeout, env=_ssh_env())
        self._persist(host, password, res)
        return res

    async def put(self, host: str, local: str, remote: str, *, timeout: float | None = None,
                  password: str | None = None) -> RemoteResult:
        pw = self._resolve(host, password)
        cap = timeout if timeout is not None else max(self._command_timeout, 300.0)  # files take longer
        if pw is not None:
            argv = ["sshpass", "-e", "scp", *_pw_opts(self._connect_timeout), local, f"{host}:{remote}"]
            res = await self._exec(host, argv, cap, env={**_ssh_env(), "SSHPASS": pw})
        else:
            argv = ["scp", *_ssh_opts(self._connect_timeout), local, f"{host}:{remote}"]
            res = await self._exec(host, argv, cap, env=_ssh_env())
        self._persist(host, password, res)
        return res

    def _resolve(self, host: str, inline: str | None) -> str | None:
        """The password to use: an inline one, else the vault's stored one, else None (→ key auth).
        Persistence happens only AFTER auth succeeds (see _persist), never on the command line."""
        if inline:
            return inline
        return self._secrets.get(password_ref(host)) if self._secrets is not None else None

    def _persist(self, host: str, inline: str | None, res: RemoteResult) -> None:
        # Save an inline password to the vault only once it actually authenticated (rc != 255), so a
        # typo'd password never overwrites a working stored one.
        if inline and self._secrets is not None and res.returncode != _SSH_FAIL:
            with contextlib.suppress(Exception):  # a vault hiccup mustn't fail an otherwise-good login
                self._secrets.set(password_ref(host), inline)

    async def _exec(self, host: str, argv: list[str], timeout: float | None,
                    env: dict[str, str] | None = None) -> RemoteResult:
        cap = timeout if timeout is not None else self._command_timeout
        try:
            rc, out, err = await run_argv(argv, timeout=cap, env=env)
        except CommandTimeout:
            return RemoteResult(host, _SSH_FAIL, "", f"{argv[0]} timed out after {cap:.0f}s")
        except FileNotFoundError as exc:
            return RemoteResult(host, _SSH_FAIL, "", str(exc))
        return RemoteResult(host, rc, out, err)


class FakeRemoteRunner:
    """In-memory ssh for CI — seeded (host, command-substring) → RemoteResult, never opens a socket."""

    def __init__(self, responses: dict[tuple[str, str], RemoteResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, str]] = []
        self.passwords: list[str | None] = []  # records the password arg, to assert redaction/routing

    async def run(self, host: str, command: str, *, timeout: float = 60.0,
                  password: str | None = None) -> RemoteResult:
        self.calls.append((host, command))
        self.passwords.append(password)
        for (h, sub), res in self._responses.items():
            if h == host and sub in command:
                return res
        return RemoteResult(host, 0, f"[fake:{host}] {command}", "")

    async def put(self, host: str, local: str, remote: str, *, timeout: float = 120.0,
                  password: str | None = None) -> RemoteResult:
        self.calls.append((host, f"put {local} -> {remote}"))
        self.passwords.append(password)
        return RemoteResult(host, 0, "", "")


def build_remote_runner(ssh_settings: Any, secrets: SecretResolver | None = None) -> RemoteRunner:
    """Pick the runner from settings — the single edge that names a concrete backend (fake in CI)."""
    if getattr(ssh_settings, "backend", "ssh") == "fake":
        return FakeRemoteRunner()
    return SshRunner(connect_timeout=getattr(ssh_settings, "connect_timeout", 10),
                     command_timeout=getattr(ssh_settings, "command_timeout", 120.0), secrets=secrets)
