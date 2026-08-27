"""Backup rotation (§52): keep the newest N, drop the rest — dumps and their config snapshots."""

from __future__ import annotations

from pathlib import Path

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
