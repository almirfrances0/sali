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

import contextlib
import shutil
from pathlib import Path

_BWRAP = "bwrap"

# Merged-/usr symlinks so binaries resolve inside the fresh sandbox root.
_MERGED_USR = (("usr/bin", "/bin"), ("usr/sbin", "/sbin"), ("usr/lib", "/lib"), ("usr/lib64", "/lib64"))


def available() -> bool:
    return shutil.which(_BWRAP) is not None


_MAX_OVERLAY_DEPTH = 3      # /var -> /var/lib -> /var/lib/dpkg is as deep as this ever needs to go
_MAX_OVERLAYS = 160         # /var/lib alone has 75 children here. At 24 the list truncated
                            # alphabetically and silently dropped /var/log, /var/tmp and
                            # /var/spool — an install would have failed for a reason nothing
                            # reported. The bound exists only to stop a pathological mount
                            # table producing an unbounded argv, so it belongs well above the
                            # real count, not near it.


def _mountpoints() -> set[str]:
    """Every mount target on this host, from /proc/self/mountinfo. Empty on any read failure, which
    degrades to the old whole-tree behaviour rather than to no sandbox."""
    out: set[str] = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 4:
                    out.add(parts[4])
    except OSError:
        return set()
    return out


def _overlay_targets(root: str, mounts: set[str] | None = None, depth: int = 0) -> list[str]:
    """Directories to overlay so that no lowerdir contains a nested mount.

    overlayfs rejects a lowerdir tree with another mount inside it ("Invalid argument"), and one
    running Docker container under /var/lib/docker is enough to kill the whole sandbox. Rather than
    naming Docker, walk down: a directory with nothing mounted beneath it is overlaid whole; one that
    has something mounted beneath it is skipped in favour of its children, and the offending child is
    simply never overlaid. /var/lib/dpkg and /var/cache/apt stay writable, which is what apt needs."""
    p = Path(root)
    if not p.is_dir() or p.is_symlink():
        return []
    mounts = _mountpoints() if mounts is None else mounts
    prefix = root.rstrip("/") + "/"
    nested = any(m.startswith(prefix) for m in mounts)
    if not nested:
        return [root]                       # the common case, and the whole of /usr and /etc
    if depth >= _MAX_OVERLAY_DEPTH:
        return []                           # too deep to keep splitting; leave it read-only
    targets: list[str] = []
    try:
        children = sorted(c for c in p.iterdir() if c.is_dir() and not c.is_symlink())
    except OSError:
        return []
    for child in children:
        targets.extend(_overlay_targets(str(child), mounts, depth + 1))
        if len(targets) >= _MAX_OVERLAYS:
            break
    return targets[:_MAX_OVERLAYS]


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
        for mnt_target in _overlay_targets(path):
            argv += ["--overlay-src", mnt_target, "--tmp-overlay", mnt_target]
    for src, dest in _MERGED_USR:
        argv += ["--symlink", src, dest]
    argv += ["--dir", "/root", "--chdir", "/root"]
    if not allow_network:
        argv += ["--unshare-net"]
    else:
        # DNS. /etc/resolv.conf is a symlink into /run on this host, and /run is a fresh tmpfs in
        # here, so the link dangles and nothing resolves — apt reaches no mirror and the sandbox
        # cannot do the one job it has. Bind the RESOLVED file (so a symlink cannot defeat it), after
        # the /etc overlay so it wins, and read-only so an experiment cannot rewrite the host's
        # resolver. Best-effort: if there is no resolv.conf to bind, the sandbox still runs.
        with contextlib.suppress(OSError):
            link = Path("/etc/resolv.conf")
            resolved = link.resolve(strict=True)
            if resolved.is_file():
                # Bind at the path the SYMLINK POINTS TO, not at /etc/resolv.conf. The overlayed /etc
                # already carries the host's symlink (-> ../run/resolvconf/resolv.conf), and bwrap
                # follows it when creating the bind target: binding onto /etc/resolv.conf fails with
                # "Can't create file ... No such file or directory" because /run is a fresh tmpfs and
                # the link dangles. Recreating the directory the link expects satisfies both.
                target = resolved if link.is_symlink() else link
                argv += ["--dir", str(target.parent), "--ro-bind", str(resolved), str(target)]
    argv += ["--"]
    argv += command
    return argv
