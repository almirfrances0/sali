"""Memory scope derivation (§30/§31): a filesystem location's project is its git repo."""

from __future__ import annotations

from pathlib import Path

from sali.core.scope import project_scope


def test_project_scope_is_the_enclosing_git_repo(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    file = tmp_path / "src" / "main.py"
    file.write_text("x", encoding="utf-8")
    expected = f"project:{tmp_path.name}"
    assert project_scope(str(file)) == expected       # a file resolves to its repo
    assert project_scope(str(tmp_path / "src")) == expected  # so does a directory within it


def test_project_scope_is_none_outside_any_repo(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert project_scope(str(plain / "notes.txt")) is None
    assert project_scope("") is None
