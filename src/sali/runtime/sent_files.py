"""Files Sali SENDS to Almir's device (Almir: "i can tell sali to send any file, zip and send any file
to me so i can download").

Almir is on his iPhone, not at the PC — a file written to a local path never reaches him. `register`
copies (or zips) the source into a served `sali-works/sends/` directory and records a row; the app then
receives it as a downloadable file via `/api/v1/files/sent/{id}/download` + a `file.sent` event.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


class SentFileStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def register(
        self, *, session_id: Any, run_id: Any, source_path: str, root: str,
        zip_it: bool = False, name: str | None = None,
    ) -> dict[str, Any]:
        """Copy (or zip) an existing file/dir into the served sends dir and record it. Returns the
        download info. Raises FileNotFoundError if the source doesn't exist."""
        src = Path(source_path).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"{source_path} does not exist")
        sends = Path(root) / "sends"
        sends.mkdir(parents=True, exist_ok=True)
        fid = uuid4()
        short = str(fid)[:8]
        if zip_it or src.is_dir():
            base = sends / f"{name or src.stem or 'files'}-{short}"
            archive = shutil.make_archive(str(base), "zip", root_dir=str(src.parent), base_dir=src.name)
            dest = Path(archive)
            filename = (name.rsplit(".", 1)[0] + ".zip") if name else dest.name
            kind = "zip"
        else:
            want = name or src.name
            suffix = Path(want).suffix or src.suffix
            stem = Path(want).stem
            dest = sends / f"{stem}-{short}{suffix}"
            shutil.copy2(src, dest)
            filename = want
            kind = "file"
        nbytes = dest.stat().st_size
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sali.sent_file (id, session_id, run_id, filename, path, bytes, kind) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                fid, session_id, run_id, filename[:200], str(dest), nbytes, kind)
        return {"id": str(fid), "filename": filename, "path": str(dest), "bytes": nbytes,
                "kind": kind, "download_url": f"/api/v1/files/sent/{fid}/download"}

    async def for_run(self, run_id: Any) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, filename, bytes, kind FROM sali.sent_file WHERE run_id = $1 "
                "ORDER BY created_at", run_id)
        return [{"id": str(r["id"]), "filename": r["filename"], "bytes": r["bytes"], "kind": r["kind"],
                 "download_url": f"/api/v1/files/sent/{r['id']}/download"} for r in rows]

    async def get(self, file_id: UUID) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT id, filename, path, bytes FROM sali.sent_file WHERE id = $1", file_id)
        return None if r is None else {"id": str(r["id"]), "filename": r["filename"],
                                       "path": r["path"], "bytes": r["bytes"]}


__all__ = ["SentFileStore"]
