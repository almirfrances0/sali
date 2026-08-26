"""The learning sandbox for ``execute_command``.

Sali runs freely on its own machine (this is its home), so ordinary commands are NOT jailed —
they run on the host. The one sandbox that remains is the *learning* sandbox: an install-capable,
throwaway environment for risky experiments. It runs the command as fake-root in a user
namespace with writable-but-ephemeral /usr, /etc, and /var, so Sali can ``apt``/``pip`` install
tools, delete, and poke around while learning — and every change is discarded on exit, the real
system untouched, the host home never mounted. If bubblewrap is unavailable the caller must
degrade explicitly (see ``exec_tool``); this module never silently runs unjailed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_BWRAP = "bwrap"

# Merged-/usr symlinks so binaries resolve inside the fresh sandbox root.
_MERGED_USR = (("usr/bin", "/bin"), ("usr/sbin", "/sbin"), ("usr/lib", "/lib"), ("usr/lib64", "/lib64"))


def available() -> bool:
    return shutil.which(_BWRAP) is not None


def build_sandbox_argv(command: list[str], *, allow_network: bool = True) -> list[str]:
    """An install-capable throwaway sandbox for LEARNING (spec fix: 'a learning sandbox').

    The command runs as fake-root in a user namespace with writable-but-ephemeral /usr, /etc,
    and /var — so it can ``apt``/``pip`` install tools, delete, and experiment freely; every
    change is discarded on exit and the real system is never touched. The host home is not
    mounted, so experiments are isolated from Sali's actual files."""
    argv: list[str] = [
        _BWRAP, "--die-with-parent", "--new-session",
        "--unshare-user", "--uid", "0", "--gid", "0",  # fake root inside the namespace
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
    ]
    for path in ("/usr", "/etc", "/var"):  # writable, ephemeral overlays
        if Path(path).exists():
            argv += ["--overlay-src", path, "--tmp-overlay", path]
    for src, dest in _MERGED_USR:
        argv += ["--symlink", src, dest]
    argv += ["--dir", "/root", "--chdir", "/root"]
    if not allow_network:
        argv += ["--unshare-net"]
    argv += ["--"]
    argv += command
    return argv
