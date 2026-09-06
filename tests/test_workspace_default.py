"""When there is no task, project writes must still land under Sali's workspace.

Almir, on watching files land across his home: "sali have a freedom to write anywhere but when working
on project he must use his folder so easy for me to track it." The gap that produced that behaviour was
narrow: PathGuard anchored relative paths at the daemon's CWD (opaque, system-managed), so bare names
were rejected and the model resorted to inventing absolute paths under ~/. This pins the correct
default and preserves the freedom that is supposed to remain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sali.config.settings import PermissionsSettings, Settings
from sali.tools.builtins.filesystem import CreateFile, ReadFile
from sali.tools.context import ToolContext
from sali.core.clock import SystemClock


def _ctx(workspace: Path, extra_write_roots: list[str] | None = None) -> ToolContext:
    """A pool-less ToolContext whose permissions.workspace points at a tmp dir.

    fs_read_roots/fs_write_roots default to the user's home, so the test only needs to add the tmp
    workspace as an allowed root - matching what production does when sali-works lives in ~/Desktop/."""
    perms = PermissionsSettings(
        workspace=str(workspace),
        fs_read_roots=[str(workspace)] + (extra_write_roots or []) + [str(workspace.parent)],
        fs_write_roots=[str(workspace)] + (extra_write_roots or []),
    )
    return ToolContext(settings=Settings(permissions=perms), clock=SystemClock())


async def test_a_bare_filename_lands_in_the_workspace(tmp_path: Path) -> None:
    """The core promise: `create_file("foo.py", ...)` with no task active writes to workspace/foo.py,
    NOT to the daemon's CWD, NOT to $HOME, NOT to /foo.py-and-rejected."""
    workspace = tmp_path / "sali-works"
    ctx = _ctx(workspace)
    result = await CreateFile().run({"path": "hello.py", "content": "print('hi')"}, ctx)
    assert result.ok, result.error
    assert (workspace / "hello.py").is_file(), "the workspace is the default landing zone"
    assert result.output["path"] == str(workspace / "hello.py")


async def test_a_nested_bare_path_creates_parents_under_the_workspace(tmp_path: Path) -> None:
    """CreateFile makes parents as needed. "my-project/main.py" is the shape that would previously
    have resolved to  (rejected) or the daemon CWD - now anchors under the
    workspace, which is the natural way a project gets started."""
    workspace = tmp_path / "sali-works"
    ctx = _ctx(workspace)
    result = await CreateFile().run({"path": "my-project/main.py", "content": "print()\n"}, ctx)
    assert result.ok, result.error
    assert (workspace / "my-project" / "main.py").is_file()


async def test_an_absolute_path_is_still_honoured(tmp_path: Path) -> None:
    """The freedom half of the answer: Almir can still name a specific place and Sali writes there.
    Without this the default becomes a cage rather than a floor."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    ctx = _ctx(tmp_path / "sali-works", extra_write_roots=[str(other)])
    absolute = other / "note.txt"
    result = await CreateFile().run({"path": str(absolute), "content": "x"}, ctx)
    assert result.ok, result.error
    assert absolute.is_file()
    assert not (tmp_path / "sali-works" / "note.txt").exists(), "must not be relocated"


async def test_the_workspace_default_matches_the_configured_root(tmp_path: Path) -> None:
    """The default anchor comes from `permissions.workspace`, not from an env-var, not from Path.home
    - so a test-configured workspace resolves under itself, and a Desktop-configured one under Desktop.
    This is what makes the fix work in production and in tests without special-casing either."""
    workspace = tmp_path / "sali-works"
    ctx = _ctx(workspace)
    guard = ctx.paths
    assert guard.base is not None
    assert Path(str(guard.base)).resolve() == workspace.resolve()


async def test_the_workspace_is_created_on_demand(tmp_path: Path) -> None:
    """A test-configured workspace does not exist until Sali needs it; the resolver must create it, or
    the very first `create_file` fails with a FileNotFound the model cannot fix."""
    workspace = tmp_path / "sali-works-fresh"
    assert not workspace.exists()
    ctx = _ctx(workspace)
    _ = ctx.paths  # accessing the property must materialise the workspace
    assert workspace.is_dir()


async def test_read_of_a_bare_name_also_resolves_under_the_workspace(tmp_path: Path) -> None:
    """The symmetry test - without it, Sali writes `foo.py` to the workspace but cannot read it back
    with the same bare name, which is exactly the failure mode PathGuard's docstring records."""
    workspace = tmp_path / "sali-works"
    ctx = _ctx(workspace)
    await CreateFile().run({"path": "seen.py", "content": "x = 1\n"}, ctx)
    result = await ReadFile().run({"path": "seen.py"}, ctx)
    assert result.ok, result.error
    assert result.output["content"] == "x = 1\n"


async def test_an_active_task_workspace_still_overrides_the_default(tmp_path: Path) -> None:
    """Precedence: the task workspace is the more specific anchor and must win when set. Anything else
    would break the mid-task behaviour the workspace guard depends on, which was the whole reason
    `PathGuard` anchored to `base` in the first place."""
    from sali.tasks.workspace import TaskWorkspace

    workspace = tmp_path / "sali-works"
    task_ws_root = tmp_path / "task-a"
    task_ws_root.mkdir()
    ctx = _ctx(workspace, extra_write_roots=[str(task_ws_root)])
    ctx.workspace = TaskWorkspace(workspace_root=task_ws_root, allowed_write_roots=[task_ws_root])

    result = await CreateFile().run({"path": "step1.py", "content": "print(1)"}, ctx)
    assert result.ok, result.error
    assert (task_ws_root / "step1.py").is_file()
    assert not (workspace / "step1.py").exists()
