"""THE SINGLE MIND — machine-wide ownership of the one authoritative Sali runtime.

    ONE PC · ONE SALI · ONE MIND

This module is the *machine-wide* answer to a question a Python singleton can never answer:
"is another Sali runtime already alive on this computer?" A ``Kernel`` memoised inside one process
says nothing about a second process; two processes each holding their own ``AgentRuntime`` are two
minds. So ownership is asserted at the OS level, not in the language.

Mechanism — ``flock(2)`` on a well-known file, held for the entire process lifetime:

* **Kernel-enforced.** The lock lives in the open file description, not in the file's bytes. Two
  processes cannot both hold it, whatever the code does.
* **Self-healing.** The kernel releases it when the holder dies — SIGKILL, OOM-kill, power loss,
  everything. There is no such thing as a stale mind lock, so recovery needs no timeout, no
  heartbeat and no reaper. Contrast the PostgreSQL ``execution_lease``, which serialises *turns*
  and needs a 300s expiry precisely because it cannot observe process death.
* **Dependency-free.** No database, no network, no daemon. It works during boot, during a Postgres
  outage, and inside recovery tooling — the moments a duplicate mind is most likely to be started.

The holder also *publishes itself*: while holding the lock it writes a small JSON record (pid, boot
id, role, API endpoint, session) into the same file. That turns the lock into a **discovery
service** — ``sali agent`` reads it to find the running Sali and attach as a client instead of
starting a second mind, so "one mind" and "many windows" are the same mechanism seen from two sides.

Liveness is read the only way that cannot race: a reader *tries* to take the lock on a fresh fd. If
it succeeds, nobody was holding it and whatever the file says is a corpse. If it fails with
``EWOULDBLOCK``, a live holder exists right now and its record is current.

This is the lowest layer on purpose (``sali.core`` imports nothing of Sali's), so the provider —
the inference boundary — can consult it without inverting the architecture.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "MindHolder",
    "MindLock",
    "ProcessRole",
    "SecondMindError",
    "current_role",
    "describe_holder",
    "held_by_this_process",
    "live_holder",
    "mind_is_live",
    "set_current_role",
]


class SecondMindError(RuntimeError):
    """Raised when a process tries to become an authoritative Sali runtime while one already lives.

    This is a *safe* failure: the caller must stop, not fall back to building its own mind.
    """

    def __init__(self, holder: MindHolder | None, attempted: str = "runtime") -> None:
        self.holder = holder
        self.attempted = attempted
        super().__init__(_second_mind_message(holder, attempted))


def _second_mind_message(holder: MindHolder | None, attempted: str) -> str:
    who = describe_holder(holder)
    return (
        f"Refusing to start a second Sali {attempted}: this machine already has one mind. {who}\n"
        "Interfaces are windows into the running Sali, never new Salis — attach with `sali agent`, "
        "the iPhone app, or the API. To take over, stop the running one first "
        "(`sudo systemctl stop sali`)."
    )


class ProcessRole(StrEnum):
    """What this OS process is allowed to be.

    ``MIND`` is the one authoritative organism: it owns the Kernel, the AgentRuntime, the AgentLoop,
    the cognitive coordinator and the inference authority. ``CLIENT`` and ``UTILITY`` never own any
    of those; the difference between them is only whether they talk to the mind (client) or to the
    datastore (utility).
    """

    MIND = "mind"        # THE Sali — owns kernel, runtime, loop, coordinator, inference
    CLIENT = "client"    # a window into the mind (terminal attach, iPhone, API client)
    UTILITY = "utility"  # datastore/inspection work only — no runtime, no cognition
    UNKNOWN = "unknown"  # not yet declared (tests, library import)


_role: ProcessRole = ProcessRole.UNKNOWN


def set_current_role(role: ProcessRole) -> None:
    """Declare what this process is. Set once, early, by the entry point."""
    global _role
    _role = role


def current_role() -> ProcessRole:
    return _role


@dataclass(frozen=True, slots=True)
class MindHolder:
    """Who the living Sali is — published by the holder, read by everyone else."""

    pid: int
    hostname: str
    boot_id: str
    started_at: float
    role: str = ProcessRole.MIND.value
    api_host: str | None = None
    api_port: int | None = None
    session_id: str | None = None
    version: str = "1"
    command: str = ""

    @property
    def api_base(self) -> str | None:
        """The local HTTP base URL of this mind's API, or None if it serves no API."""
        if not self.api_port:
            return None
        host = self.api_host or "127.0.0.1"
        if host in ("0.0.0.0", "::", ""):  # noqa: S104 - normalising a bind address for a client URL
            host = "127.0.0.1"
        return f"http://{host}:{self.api_port}"

    @property
    def uptime_s(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def describe_holder(holder: MindHolder | None) -> str:
    if holder is None:
        return "No Sali runtime is currently alive on this machine."
    where = holder.api_base or "no API"
    return (f"The living Sali is pid {holder.pid} on {holder.hostname} "
            f"({holder.role}, up {int(holder.uptime_s)}s, {where}).")


# ── Where the lock lives ──────────────────────────────────────────────────────────────────────────
# The path MUST resolve identically in every Sali process or the guarantee evaporates: two processes
# flocking two different files are two happy, independent minds.
#
# So it is derived from the *passwd database*, not the environment. In particular it must NOT use
# $XDG_RUNTIME_DIR: a systemd SYSTEM unit (`sali.service`, which is what `User=almir` + `sudo
# systemctl start sali` gives you) has no XDG_RUNTIME_DIR, while an interactive terminal does — the
# daemon and the terminal would have resolved different files and each become "the only Sali".
# `pw_dir` is likewise immune to a stray `HOME=` in a unit file or a `sudo -E`.
#
# The file therefore survives reboots. That is harmless: liveness is decided by flock, never by the
# file's contents, and `boot_id` marks records written before the current boot.

_ENV_OVERRIDE = "SALI_MIND_LOCK"  # tests and containers point this at their own path


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _home_dir() -> Path:
    """This uid's home from the passwd database — stable regardless of $HOME."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, ImportError, OSError):
        return Path.home()


def _candidate_dirs() -> list[Path]:
    return [
        _home_dir() / ".local" / "state" / "sali",
        Path(f"/tmp/sali-{os.getuid()}"),  # noqa: S108 - uid-namespaced, mode 0700, last resort
    ]


def default_lock_path() -> Path:
    """The one path every Sali process running as this user agrees on."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        p = Path(override)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    for d in _candidate_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            probe = d / ".writable"
            probe.touch()
            probe.unlink(missing_ok=True)
            return d / "mind.lock"
        except OSError:
            continue
    return Path(f"/tmp/sali-{os.getuid()}-mind.lock")  # noqa: S108 - uid-namespaced final fallback


class MindLock:
    """Machine-wide exclusive ownership of the authoritative Sali runtime.

    Acquire once at process start; hold for the whole life of the process; never release except at
    a deliberate shutdown. The fd is kept on the instance *and* in a module-level registry so a
    stray garbage collection can never silently drop this machine's guarantee.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_lock_path()
        self._fd: int | None = None

    # ── Ownership ────────────────────────────────────────────────────────────────────────────────
    @property
    def held(self) -> bool:
        """True only if *this* process currently holds the machine-wide lock."""
        return self._fd is not None

    def acquire(
        self,
        *,
        role: ProcessRole = ProcessRole.MIND,
        api_host: str | None = None,
        api_port: int | None = None,
        session_id: str | None = None,
        command: str = "",
    ) -> bool:
        """Try to become THE Sali. Returns False if another live process already is.

        Never blocks and never waits: a second mind must fail immediately and visibly, not queue up
        behind the first one hoping to inherit the machine.
        """
        if self._fd is not None:
            self.publish(api_host=api_host, api_port=api_port, session_id=session_id)
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return False
        self._fd = fd
        _HELD_FDS.add(fd)  # anchor: never let GC close the fd and hand the machine to a second mind
        set_current_role(role)
        self._write(MindHolder(
            pid=os.getpid(), hostname=socket.gethostname(), boot_id=_boot_id(),
            started_at=time.time(), role=role.value, api_host=api_host, api_port=api_port,
            session_id=session_id, command=command or _self_command()))
        return True

    def acquire_or_raise(self, **kw: Any) -> None:
        """Become THE Sali or fail safely — the guard every authoritative entry point should use."""
        if not self.acquire(**kw):
            raise SecondMindError(live_holder(self.path), attempted=str(kw.get("role", "runtime")))

    def publish(
        self, *, api_host: str | None = None, api_port: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """Update the published record (e.g. once the API has actually bound its port).

        Only the holder may publish; for anyone else this is a no-op, because the record must always
        describe the process the kernel says owns the lock.
        """
        if self._fd is None:
            return
        cur = self._read_record() or MindHolder(
            pid=os.getpid(), hostname=socket.gethostname(), boot_id=_boot_id(),
            started_at=time.time(), role=current_role().value)
        self._write(MindHolder(
            pid=os.getpid(), hostname=cur.hostname, boot_id=cur.boot_id,
            started_at=cur.started_at, role=cur.role,
            api_host=api_host if api_host is not None else cur.api_host,
            api_port=api_port if api_port is not None else cur.api_port,
            session_id=session_id if session_id is not None else cur.session_id,
            version=cur.version, command=cur.command))

    def release(self) -> None:
        """Give up ownership. Idempotent. Only a deliberate shutdown should call this."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        _HELD_FDS.discard(fd)
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        if current_role() is ProcessRole.MIND:
            set_current_role(ProcessRole.UNKNOWN)

    # ── Internals ────────────────────────────────────────────────────────────────────────────────
    def _write(self, holder: MindHolder) -> None:
        if self._fd is None:
            return
        blob = json.dumps(holder.to_dict()).encode("utf-8")
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, blob)
            os.fsync(self._fd)
        except OSError:
            pass  # the lock — not the record — is the guarantee; a failed write never breaks it

    def _read_record(self) -> MindHolder | None:
        return _read_record(self.path)

    def __enter__(self) -> MindLock:
        self.acquire_or_raise()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# Anchors every acquired fd for the life of the process. Closing an fd releases its flock, so an
# accidental drop of a MindLock reference must never be able to release this machine's ownership.
_HELD_FDS: set[int] = set()


def _self_command() -> str:
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
        return " ".join(p for p in raw.decode("utf-8", "replace").split("\0") if p)[:200]
    except OSError:
        return ""


def _read_record(path: Path) -> MindHolder | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = {f: data.get(f) for f in MindHolder.__slots__ if f in data}
    try:
        return MindHolder(**fields)  # type: ignore[arg-type]
    except TypeError:
        return None


def live_holder(path: Path | str | None = None) -> MindHolder | None:
    """The mind that is alive on this machine *right now*, or None.

    Liveness is decided by the kernel, never by the file's contents: we open a fresh fd and try to
    take the lock. Success means nobody held it, so any record present is a corpse and we say None.
    Failure with EWOULDBLOCK means a live holder exists at this instant, so its record is current.
    (A process that holds the lock sees its own record here, because flock conflicts across
    separate open file descriptions in the same process too.)
    """
    p = Path(path) if path is not None else default_lock_path()
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return _read_record(p)          # someone holds it — the record describes a live mind
        fcntl.flock(fd, fcntl.LOCK_UN)      # we got it, so nobody owns it — the file is a corpse
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def mind_is_live(path: Path | str | None = None) -> bool:
    """Is an authoritative Sali runtime alive on this machine (in any process)?"""
    return live_holder(path) is not None


def held_by_this_process() -> bool:
    """Is *this* process the authoritative mind? The check every gate should ask."""
    return current_role() is ProcessRole.MIND and bool(_HELD_FDS)
