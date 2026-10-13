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


def _resolve(path: str | Path, base: Path | None = None) -> Path:
    # strict=False so not-yet-existing files (create_file) still resolve their parent chain.
    #
    # A RELATIVE path resolves against `base` (the active task workspace) rather than the daemon's
    # working directory. Without this, Sali asking for "app.py" while working in a task looked in
    # whatever directory the service happened to start in and got FILE_NOT_FOUND for a file he had
    # just written — so he could not see his own work, kept trying to recreate it, and never advanced
    # past the step. The workspace is the working directory; this makes that true for tools too.
    p = Path(path).expanduser()
    if base is not None and not p.is_absolute():
        p = Path(base) / p
    return p.resolve()


def _under(target: Path, roots: list[Path]) -> bool:
    return any(target == root or root in target.parents for root in roots)


class PathGuard:
    def __init__(self, perms: PermissionsSettings, base: Path | None = None) -> None:
        # `base` is the active task workspace, used to anchor relative paths (see _resolve).
        self.base = Path(base).expanduser().resolve() if base else None
        self.read_roots = [_resolve(p) for p in perms.fs_read_roots]
        self.write_roots = [_resolve(p) for p in perms.fs_write_roots]
        self.deny = [_resolve(p) for p in perms.fs_deny]
        self.readonly = [_resolve(p) for p in perms.fs_readonly]

    def _check(self, path: str | Path, roots: list[Path], mode: str) -> Path:
        resolved = _resolve(path, self.base)
        if _under(resolved, self.deny):
            raise PathViolation(f"{mode} denied: {resolved} is under a protected path")
        if mode == "write" and _under(resolved, self.readonly):
            raise PathViolation(
                f"write denied: {resolved} is inside Sali's own installation (read-only — Sali can "
                "inspect its code but not modify its runtime)")
        if not _under(resolved, roots):
            raise PathViolation(f"{mode} denied: {resolved} is outside the allowed roots")
        return resolved

    def check_read(self, path: str | Path) -> Path:
        return self._check(path, self.read_roots, "read")

    def check_write(self, path: str | Path) -> Path:
        # Sali writes freely in its home (including its own repos); off-limits are fs_deny
        # (credentials, salix) and fs_readonly (its own installation) — both enforced in _check.
        return self._check(path, self.write_roots, "write")
