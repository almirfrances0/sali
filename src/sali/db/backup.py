"""Local backup + rotation (spec §52).

Sali's memory is important, so make it recoverable — locally, no cloud. Two parts, kept SEPARATE:
  1. the datastore (memory, graph, events, everything) via ``pg_dump`` in the compressed custom
     format. It holds NO raw secrets (the golden rule: Postgres stores only secret REFERENCES), so
     it's protected by 0600, not re-encryption.
  2. the config + the secret VAULT, copied aside. The vault is ALREADY Fernet-encrypted at rest, so
     it stays encrypted in the backup; its key is copied alongside so a restore works — which is why
     the whole backup directory is 0700 and the caller is told to keep it as safely as ~/.config/sali.
Old backups are rotated so the directory can't grow without bound.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CONFIG = Path.home() / ".config" / "sali"
_ARTIFACTS = ("vault.json", "vault.key", "sali.toml")  # the vault (encrypted), its key, the config


def default_dir() -> Path:
    return Path.home() / ".local" / "share" / "sali" / "backups"


def _rotate(dest: Path, keep: int) -> int:
    """Keep the newest `keep` datastore dumps and their config snapshots; drop the rest. Returns how
    many backups were removed."""
    dumps = sorted(dest.glob("sali-*.dump"))
    stale = dumps[:-keep] if keep > 0 else []
    for dump in stale:
        stamp = dump.name[len("sali-"):-len(".dump")]
        dump.unlink(missing_ok=True)
        shutil.rmtree(dest / f"config-{stamp}", ignore_errors=True)
    return len(stale)


def run_backup(settings: Any, dest: Path | None = None, *, keep: int = 7,
               timeout: float = 600.0) -> dict[str, Any]:
    """Dump the datastore + copy the config/vault, then rotate. Returns a summary. Raises on failure
    (a backup that half-ran should be loud, not silent)."""
    dest = dest or default_dir()
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    # UTC, like every other instant in this system. The old comment argued local time was "the right
    # label here", and on a host whose zone matches its owner that is arguable — but this host is set
    # to America/New_York while Almir works in Africa/Dar_es_Salaam, so a backup labelled 13:00
    # happened at 20:00 his time. Local time also repeats an hour every DST fall-back, which is a
    # genuine collision for a filename that identifies a restore point. Same format, so the existing
    # glob and the lexicographic-is-chronological rotation are unaffected.
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")

    # Dump to a temp sibling first, then atomically rename — so a failed/timed-out pg_dump never
    # leaves a truncated file under the real backup name (a restore must never find a corrupt dump).
    dump = dest / f"sali-{stamp}.dump"
    partial = dest / f".sali-{stamp}.dump.partial"
    try:
        subprocess.run(  # noqa: S603,S607 - fixed argv, peer auth over the unix socket, own machine
            ["pg_dump", "-d", settings.db.name, "-Fc", "-f", str(partial)],
            check=True, capture_output=True, text=True, timeout=timeout)
        os.chmod(partial, 0o600)
        os.replace(partial, dump)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(partial)
        raise

    conf = dest / f"config-{stamp}"
    conf.mkdir(mode=0o700, exist_ok=True)
    copied: list[str] = []
    for name in _ARTIFACTS:
        src = _CONFIG / name
        if src.exists():
            shutil.copy2(src, conf / name)
            os.chmod(conf / name, 0o600)
            copied.append(name)

    rotated = _rotate(dest, keep)
    return {
        "dir": str(dest), "dump": str(dump), "dump_bytes": dump.stat().st_size,
        "config": copied, "rotated": rotated,
        "has_secret_key": "vault.key" in copied,  # so the caller can warn about protecting the dir
    }
