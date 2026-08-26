"""PathGuard — filesystem scoping.

Every path a tool touches is realpath-resolved first (which collapses ``..`` and follows
symlinks, defeating escape attempts), then checked: it must sit under an allowed root and
must NOT sit under any deny prefix. Deny always wins. Read and write have separate root sets
so a tool can be allowed to read broadly but write only into the project (fix H2 substrate).
"""

from __future__ import annotations

from pathlib import Path

from sali.config.settings import PermissionsSettings


class PathViolation(Exception):
    """A path is outside the allowed roots or inside a denied prefix."""


def _resolve(path: str | Path) -> Path:
    # strict=False so not-yet-existing files (create_file) still resolve their parent chain.
    return Path(path).expanduser().resolve()


def _under(target: Path, roots: list[Path]) -> bool:
    return any(target == root or root in target.parents for root in roots)


class PathGuard:
    def __init__(self, perms: PermissionsSettings) -> None:
        self.read_roots = [_resolve(p) for p in perms.fs_read_roots]
        self.write_roots = [_resolve(p) for p in perms.fs_write_roots]
        self.deny = [_resolve(p) for p in perms.fs_deny]

    def _check(self, path: str | Path, roots: list[Path], mode: str) -> Path:
        resolved = _resolve(path)
        if _under(resolved, self.deny):
            raise PathViolation(f"{mode} denied: {resolved} is under a protected path")
        if not _under(resolved, roots):
            raise PathViolation(f"{mode} denied: {resolved} is outside the allowed roots")
        return resolved

    def check_read(self, path: str | Path) -> Path:
        return self._check(path, self.read_roots, "read")

    def check_write(self, path: str | Path) -> Path:
        resolved = self._check(path, self.write_roots, "write")
        # Never write inside a git repo's control dir — blocks .git/config (core.sshCommand
        # RCE) and .git/hooks persistence, even if a repo sits under a write root (red-team #2).
        if ".git" in resolved.parts:
            raise PathViolation(f"write denied: {resolved} is inside a .git directory")
        return resolved
