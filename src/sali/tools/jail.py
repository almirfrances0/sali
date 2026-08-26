"""A bubblewrap jail for ``execute_command``.

The command runs inside a kernel-namespace sandbox: read-only binds for the binaries and the
*narrow* set of dirs the caller explicitly allows (NOT the broad read roots — red-team #4),
every deny prefix masked with a tmpfs, a private /proc and /tmp, no network by default, all
capabilities dropped, and no access to anything not bound (fix H2). If bubblewrap is
unavailable the caller must degrade explicitly — this module never silently runs unjailed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_BWRAP = "bwrap"

# Read-only system paths a typical diagnostic binary needs to load and run.
_SYSTEM_ROBINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")


def available() -> bool:
    return shutil.which(_BWRAP) is not None


def build_argv(
    command: list[str],
    *,
    read_binds: list[str],
    cwd: str,
    allow_network: bool = False,
    deny: list[str] | None = None,
) -> list[str]:
    """Wrap ``command`` in a bwrap invocation. Assumes :func:`available` was checked.

    ``read_binds`` are the ONLY non-system directories mounted (read-only); ``deny`` prefixes
    that fall under a bound dir are shadowed with a tmpfs (later mount wins in bwrap)."""
    argv: list[str] = [
        _BWRAP,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--cap-drop", "ALL",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    if not allow_network:
        argv += ["--unshare-net"]

    bound: list[Path] = []
    for path in _SYSTEM_ROBINDS:
        p = Path(path)
        if p.exists():
            argv += ["--ro-bind", path, path]
            bound.append(p)
    for root in read_binds:
        resolved = Path(root).expanduser().resolve()
        if resolved.exists():
            argv += ["--ro-bind", str(resolved), str(resolved)]
            bound.append(resolved)

    # Mask any deny prefix that is actually reachable under a bound dir (e.g. /etc/shadow under
    # the /etc bind). Dirs → tmpfs; files → bound over /dev/null so a read returns nothing.
    for denied in deny or []:
        resolved = Path(denied).expanduser().resolve()
        if not resolved.exists() or not any(resolved == b or b in resolved.parents for b in bound):
            continue
        if resolved.is_dir():
            argv += ["--tmpfs", str(resolved)]
        else:
            argv += ["--ro-bind", "/dev/null", str(resolved)]

    argv += ["--chdir", cwd, "--"]
    argv += command
    return argv
