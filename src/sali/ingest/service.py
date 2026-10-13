"""IngestService — turn a file into recallable, citeable memories (§44 document ingestion).

Explicit ingestion (Almir/Sali chose it) so §11's "live info never auto-persists" is respected. Each
chunk becomes a FILE_OBSERVATION memory with provenance ("from <path> — chunk i/N") and is REDACTED
first (a secret in a doc must not land in memory). Re-ingesting is idempotent by content hash: an
unchanged file is a no-op; a changed file closes its old chunks and writes fresh ones.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.scope import project_scope
from sali.ingest.chunker import chunk_text
from sali.ingest.extractors import extract_text
from sali.ingest.models import IngestResult
from sali.memory import embed_worker
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger
from sali.provider.base import ModelProvider
from sali.security.redact import redact

log = get_logger("sali.ingest")

_STATUS_DETAIL = {
    "needs_pypdf": "install pypdf to ingest PDFs: pip install pypdf",
    "unsupported": "unsupported file type",
    "empty": "no extractable text in the file",
    "error": "could not read the file",
}


class IngestService:
    def __init__(self, pool: Any, provider: ModelProvider) -> None:
        self.pool = pool
        self.provider = provider

    async def ingest(self, path: str) -> IngestResult:
        p = Path(path).expanduser()
        if not p.is_file():
            return IngestResult(path=str(p), status="error", detail="not a file")

        content_hash, size = _hash_file(p)
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, content_hash FROM document WHERE path = $1", str(p))
        if existing and existing["content_hash"] == content_hash:
            return IngestResult(path=str(p), status="unchanged", bytes=size,
                                detail="already ingested, unchanged")

        text, status = extract_text(p)
        if status != "ok":
            return IngestResult(path=str(p), status=status, bytes=size,
                                detail=_STATUS_DETAIL.get(status))
        chunks = chunk_text(text)
        if not chunks:
            return IngestResult(path=str(p), status="empty", bytes=size)

        n = len(chunks)
        async with self.pool.acquire() as conn, conn.transaction():
            if existing:
                doc_id = existing["id"]
                await conn.execute(  # a changed file: retire the old chunks, then write fresh
                    "UPDATE memory SET valid_until = now() "
                    "WHERE claim_key LIKE $1 AND valid_until IS NULL", f"doc:{doc_id}#%")
            else:
                doc_id = await conn.fetchval(
                    "INSERT INTO document (path, content_hash, bytes) VALUES ($1, $2, $3) RETURNING id",
                    str(p), content_hash, size)
            scope = project_scope(str(p)) or "global"  # a project's docs are scoped to it (§31)
            for i, chunk in enumerate(chunks):
                await memory_writer.remember(
                    conn, layer=MemoryLayer.SEMANTIC, content=redact(chunk),
                    source=MemorySource.FILE_OBSERVATION, note=f"from {p} — chunk {i + 1}/{n}",
                    importance=0.5, obs_conf=0.9, functional=True, claim_key=f"doc:{doc_id}#{i}",
                    scope=scope)
            await conn.execute(
                "UPDATE document SET content_hash = $1, bytes = $2, chunks = $3, status = 'ok', "
                "ingested_at = now() WHERE id = $4", content_hash, size, n, doc_id)

        await embed_worker.embed_pending(self.pool, self.provider)  # make the chunks recallable now
        log.info("ingested", path=str(p), chunks=n)
        return IngestResult(path=str(p), status="ok", chunks=n, bytes=size)


def _hash_file(p: Path) -> tuple[str, int]:
    """Stream the file for its sha256 + size, so a large file never loads whole into RAM."""
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as fh:
        while block := fh.read(65536):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size
