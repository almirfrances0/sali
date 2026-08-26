"""Filesystem tools, every path confined by the PathGuard (fix H2).

Reads (R0) are bounded to the read roots; writes (R1) to the write roots; both are hard-denied
inside protected prefixes (~/.ssh, the config, the off-limits salix project, …). Nothing here
can escape those roots because every path is realpath-resolved before the check.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.pathguard import PathViolation
from sali.tools.registry import ToolRegistry

_MAX_BYTES = 64 * 1024
_PATH_ARG = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


class ListDir(Tool):
    name = "list_directory"
    description = "List the entries of a directory (name, type, size)."
    parameters = _PATH_ARG
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.paths.check_read(args["path"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        if not path.is_dir():
            return ToolResult(ok=False, display="not a directory", error=f"{path} is not a directory")
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: p.name)[:500]:
            kind = "dir" if child.is_dir() else "file"
            size = child.stat().st_size if child.is_file() else None
            entries.append({"name": child.name, "type": kind, "size": size})
        return ToolResult(ok=True, output={"path": str(path), "entries": entries},
                          display=f"{len(entries)} entries in {path}")


class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file (up to 64 KiB)."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "max_bytes": {"type": "integer"}},
        "required": ["path"],
    }
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.paths.check_read(args["path"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        if not path.is_file():
            return ToolResult(ok=False, display="not a file", error=f"{path} is not a file")
        limit = min(int(args.get("max_bytes") or _MAX_BYTES), _MAX_BYTES)
        raw = path.read_bytes()
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", "replace")
        return ToolResult(
            ok=True,
            output={"path": str(path), "content": text, "truncated": truncated, "bytes": len(raw)},
            display=f"read {min(len(raw), limit)} bytes from {path.name}",
        )


class FileMetadata(Tool):
    name = "file_metadata"
    description = "Stat a path: size, mode, mtime, and type."
    parameters = _PATH_ARG
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.paths.check_read(args["path"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        if not path.exists():
            return ToolResult(ok=False, display="not found", error=f"{path} does not exist")
        st = path.stat()
        out = {
            "path": str(path), "size": st.st_size, "mode": oct(st.st_mode & 0o777),
            "mtime": int(st.st_mtime), "is_dir": path.is_dir(), "is_file": path.is_file(),
            "is_symlink": path.is_symlink(),
        }
        return ToolResult(ok=True, output=out, display=f"{out['size']} bytes, mode {out['mode']}")


class SearchFiles(Tool):
    name = "search_files"
    description = "Find files under a root matching a glob pattern."
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "pattern": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["root", "pattern"],
    }
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            root = ctx.paths.check_read(args["root"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        limit = min(int(args.get("max_results") or 100), 500)
        matches = []
        for match in root.rglob(str(args["pattern"])):
            matches.append(str(match))
            if len(matches) >= limit:
                break
        return ToolResult(ok=True, output={"root": str(root), "matches": matches},
                          display=f"{len(matches)} matches")


class CreateFile(Tool):
    name = "create_file"
    description = "Create a new file with the given text content (refuses to overwrite)."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.paths.check_write(args["path"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        if path.exists():
            return ToolResult(ok=False, display="exists", error=f"{path} already exists (use modify_file)")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(args["content"])
        path.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, output={"path": str(path), "bytes": len(content.encode())},
                          display=f"created {path.name}")


class ModifyFile(Tool):
    name = "modify_file"
    description = "Overwrite an existing file's content (optimistic-locked on its current sha256)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "expected_sha256": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.paths.check_write(args["path"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        if not path.is_file():
            return ToolResult(ok=False, display="not a file", error=f"{path} is not an existing file")
        expected = args.get("expected_sha256")
        if expected:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current != expected:
                return ToolResult(ok=False, display="stale", error="file changed since it was read")
        content = str(args["content"])
        path.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, output={"path": str(path), "bytes": len(content.encode())},
                          display=f"modified {path.name}")


def register_builtins(registry: ToolRegistry) -> None:
    for cls in (ListDir, ReadFile, FileMetadata, SearchFiles, CreateFile, ModifyFile):
        registry.register(cls())
