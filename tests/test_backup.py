"""Backup rotation (§52): keep the newest N, drop the rest — dumps and their config snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sali.db import backup
from sali.db.backup import _rotate

_STAMPS = ["20260101-000000", "20260102-000000", "20260103-000000",
           "20260104-000000", "20260105-000000"]


def test_rotation_keeps_the_newest_and_drops_their_config(tmp_path: Path) -> None:
    for ts in _STAMPS:
        (tmp_path / f"sali-{ts}.dump").write_text("x", encoding="utf-8")
        (tmp_path / f"config-{ts}").mkdir()

    removed = _rotate(tmp_path, keep=3)

    assert removed == 2
    kept = sorted(p.name for p in tmp_path.glob("sali-*.dump"))
    assert kept == [f"sali-{s}.dump" for s in _STAMPS[-3:]]  # the three newest survive
    assert not (tmp_path / "config-20260101-000000").exists()  # the oldest config snapshot is gone
    assert (tmp_path / "config-20260105-000000").is_dir()      # the newest survives


def test_rotation_is_a_noop_when_within_the_limit(tmp_path: Path) -> None:
    for ts in _STAMPS[:2]:
        (tmp_path / f"sali-{ts}.dump").write_text("x", encoding="utf-8")
    assert _rotate(tmp_path, keep=7) == 0
    assert len(list(tmp_path.glob("sali-*.dump"))) == 2


def test_failed_dump_leaves_no_corrupt_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pg_dump that writes some bytes then dies must NOT leave a truncated file under the real
    # backup name — a restore must never find a half-written dump. The .partial is cleaned up.
    def _boom(argv: list[str], **_: object) -> None:
        target = Path(argv[argv.index("-f") + 1])
        target.write_text("half a dump", encoding="utf-8")  # pg_dump got partway then failed
        raise subprocess.CalledProcessError(1, argv, stderr="disk full")

    monkeypatch.setattr("sali.db.backup.subprocess.run", _boom)
    settings = SimpleNamespace(db=SimpleNamespace(name="sali_test"))

    with pytest.raises(subprocess.CalledProcessError):
        backup.run_backup(settings, tmp_path)

    assert list(tmp_path.glob("sali-*.dump")) == []          # no final dump under the real name
    assert list(tmp_path.glob(".sali-*.partial")) == []      # the partial was removed
