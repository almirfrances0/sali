"""TaskWorkspace — deterministic filesystem boundary for tasks.

When a user says "Create a website in folder example", the system resolves that folder
to an absolute path and binds it to the task. All filesystem writes for that task must
land inside the workspace boundary. The LLM may propose paths; the execution layer
decides whether they're allowed.

PathGuard already handles realpath resolution and symlink following — this module adds
per-task workspace scoping on top of that existing foundation.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.workspace")

# Patterns that extract a folder reference from user input.
# "Create a website in folder example" → "example"
# "Build the project at ~/Desktop/myapp" → "~/Desktop/myapp"
# "Work in /tmp/test" → "/tmp/test"
_FOLDER_PATTERNS = [
    re.compile(r"\bin\s+(?:folder|directory|dir|path|project)\s+[\"']?([^\s\"']+)[\"']?", re.IGNORECASE),
    re.compile(r"\bat\s+(?:folder|directory|dir|path|project)\s+[\"']?([^\s\"']+)[\"']?", re.IGNORECASE),
    re.compile(r"\bin\s+[\"']?([~/][^\s\"']+)[\"']?", re.IGNORECASE),
    re.compile(r"\bat\s+[\"']?([~/][^\s\"']+)[\"']?", re.IGNORECASE),
    re.compile(r"(?:folder|directory|dir|project)\s+[\"']?([^\s\"']+)[\"']?", re.IGNORECASE),
]


@dataclass(slots=True)
class TaskWorkspace:
    """The deterministic filesystem boundary for a task.

    workspace_root: the resolved absolute path that is the task's home.
    allowed_write_roots: paths the task may write to (initially just workspace_root).
    """
    workspace_root: Path
    allowed_write_roots: list[Path]

    def contains(self, path: Path) -> bool:
        """Check if a resolved path is inside any allowed write root.

        Uses Path.is_relative_to which handles the boundary correctly (no string prefix matching).
        """
        return any(
            path == root or _is_relative_to(path, root)
            for root in self.allowed_write_roots
        )

    def resolve_relative(self, path: str) -> Path:
        """Resolve a relative path against the workspace root.

        "src/main.ts" → /workspace_root/src/main.ts
        "/absolute/path" → /absolute/path (unchanged)
        """
        p = Path(path)
        if p.is_absolute():
            return p
        return (self.workspace_root / p).resolve()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": str(self.workspace_root),
            "allowed_write_roots": [str(r) for r in self.allowed_write_roots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskWorkspace | None:
        if not data or not data.get("workspace_root"):
            return None
        return cls(
            workspace_root=Path(data["workspace_root"]),
            allowed_write_roots=[Path(r) for r in data.get("allowed_write_roots", [data["workspace_root"]])],
        )


def _is_relative_to(path: Path, base: Path) -> bool:
    """Check if path is relative to base, compatible with Python 3.8+."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


# Directories that must NEVER be a task's working root (Prompt 5 §3). These are system/home roots —
# treating one as the workspace is how task-generated files scatter across /home. A SUBDIRECTORY of
# home (e.g. /home/almir/projects/myapp — a real project the user referenced) is fine; only the bare
# roots are rejected. Explicit user requests can still TOUCH files anywhere via the free exec path.
_SYSTEM_ROOTS = frozenset(
    Path(p) for p in (
        "/", "/home", "/etc", "/usr", "/var", "/bin", "/sbin", "/lib", "/lib64", "/boot",
        "/root", "/dev", "/proc", "/sys", "/run", "/opt", "/srv", "/mnt", "/media",
    )
)


def is_safe_workspace_root(path: Path) -> bool:
    """A path is a safe task workspace root unless it IS a system/home root (§3). Subdirectories of
    those are allowed — only the bare roots (and the user's home dir itself) are rejected."""
    p = path.expanduser()
    with contextlib.suppress(OSError):
        p = p.resolve()
    return p not in _SYSTEM_ROOTS and p != Path.home()


@dataclass(slots=True)
class WorkspaceResolution:
    """The outcome of deterministic workspace resolution (Prompt 5 §4)."""
    workspace: TaskWorkspace
    mode: str            # explicit | inherited | auto
    rejected: str | None  # a user-referenced path refused as unsafe (fell back to auto), else None


def resolve_task_workspace(
    *, objective: str, explicit: str | None, active_task_workspace: str | None,
    sali_works_root: str, task_id: UUID, cwd: str | None = None,
    referenced_paths: list[str] | None = None,
) -> WorkspaceResolution:
    """Deterministically resolve a task's authoritative workspace, by strict priority (§1/§4):

      1. the existing active-task workspace (durable — NEVER re-resolved on continuation/recovery),
      2. an explicit workspace the user gave,
      3. an explicit folder referenced in the objective / a referenced project path,
      4. a deterministic auto-workspace under <sali-works>/tasks/<task_id>/.

    A user-referenced path that is a system/home root is rejected (§3) and resolution falls back to (4);
    the rejected path is reported so the caller can emit workspace.rejected. Directories are created so
    the workspace exists on disk. This runs ONCE at task creation — the result becomes durable state."""
    home = Path.home()
    cwd_base = Path(cwd).expanduser() if cwd else home

    # 1. Existing task workspace wins — durable identity, never re-resolved.
    if active_task_workspace:
        p = Path(active_task_workspace)
        return WorkspaceResolution(TaskWorkspace(p, [p]), "inherited", None)

    rejected: str | None = None

    # 2 & 3. Explicit user workspace, then a folder referenced in the objective / referenced paths.
    candidate: Path | None = None
    for raw in (explicit, extract_folder_reference(objective), *(referenced_paths or [])):
        if not raw:
            continue
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = cwd_base / p
        with contextlib.suppress(OSError):
            p = p.resolve()
        if is_safe_workspace_root(p):
            candidate = p
            break
        rejected = str(p)  # remember the refused root, keep looking / fall back to auto

    if candidate is not None:
        with contextlib.suppress(OSError):
            candidate.mkdir(parents=True, exist_ok=True)
        return WorkspaceResolution(TaskWorkspace(candidate, [candidate]), "explicit", rejected)

    # 4. Deterministic auto-workspace beneath sali-works — durable, structured, never /home directly.
    auto = (Path(sali_works_root).expanduser() / "tasks" / str(task_id))
    with contextlib.suppress(OSError):
        auto.mkdir(parents=True, exist_ok=True)
    return WorkspaceResolution(TaskWorkspace(auto, [auto]), "auto", rejected)


def resolve_workspace(user_input: str, cwd: str) -> TaskWorkspace | None:
    """Deterministically resolve a workspace from user input.

    Returns None if no workspace folder is specified in the input.
    The cwd is used to resolve relative paths.
    """
    folder = extract_folder_reference(user_input)
    if folder is None:
        return None

    # Resolve to absolute path
    p = Path(folder).expanduser()
    if not p.is_absolute():
        p = Path(cwd).expanduser() / p

    # Resolve symlinks and .. (reuse PathGuard's approach)
    resolved = p.resolve()

    # Create the directory if it doesn't exist yet (the user declared it as the workspace)
    resolved.mkdir(parents=True, exist_ok=True)

    log.info("workspace_resolved", input_folder=folder, resolved=str(resolved))
    return TaskWorkspace(
        workspace_root=resolved,
        allowed_write_roots=[resolved],
    )


def extract_folder_reference(user_input: str) -> str | None:
    """Extract a folder reference from user input using deterministic patterns.

    Returns the raw folder string (may be relative), or None if no folder reference found.
    """
    for pattern in _FOLDER_PATTERNS:
        m = pattern.search(user_input or "")
        if m:
            return m.group(1)
    return None


@dataclass(slots=True)
class ArtifactRecord:
    """A file that was deterministically observed as created/modified by a tool execution."""
    task_id: UUID
    artifact_path: str
    artifact_type: str  # "created" | "modified"
