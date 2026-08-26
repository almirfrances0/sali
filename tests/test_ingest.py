"""Document ingestion (§44): the chunker, the extractors, and the end-to-end service + tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sali.config.settings import PermissionsSettings, Settings
from sali.core.clock import SystemClock
from sali.ingest.chunker import chunk_text
from sali.ingest.extractors import extract_text
from sali.ingest.service import IngestService
from sali.provider.fake import FakeModelProvider
from sali.tools.builtins.ingest_tool import IngestDocument
from sali.tools.context import ToolContext


# ---- chunker (pure) ----------------------------------------------------------------------------
def test_chunker_splits_with_overlap_and_handles_edges() -> None:
    assert chunk_text("") == []
    assert chunk_text("short") == ["short"]
    big = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(20))
    chunks = chunk_text(big, size=1200, overlap=150)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 + 150 for c in chunks)  # honours the window (+overlap carry)
    assert "paragraph 0" in chunks[0] and "paragraph 19" in chunks[-1]


# ---- extractors (pure) -------------------------------------------------------------------------
def test_extractors_text_unsupported_and_pdf(tmp_path: Path) -> None:
    txt = tmp_path / "doc.md"
    txt.write_text("# Title\n\nsome content")
    assert extract_text(txt) == ("# Title\n\nsome content", "ok")

    (tmp_path / "img.png").write_bytes(b"\x89PNG")
    assert extract_text(tmp_path / "img.png") == ("", "unsupported")

    (tmp_path / "empty.txt").write_text("   ")
    assert extract_text(tmp_path / "empty.txt") == ("", "empty")

    # pypdf isn't installed → the PDF path reports it cleanly instead of crashing.
    (tmp_path / "f.pdf").write_bytes(b"%PDF-1.4 ...")
    assert extract_text(tmp_path / "f.pdf")[1] in ("needs_pypdf", "ok", "error")


# ---- service (DB, end to end) ------------------------------------------------------------------
@pytest.mark.db
async def test_ingest_creates_recallable_redacted_memories(live_pool: Any) -> None:
    doc = _tmp_doc(live_pool, "notes.md", "Almir's plan.\n\nContact leaked: secret@example.com here.")
    svc = IngestService(live_pool, FakeModelProvider())
    result = await svc.ingest(doc)

    assert result.status == "ok" and result.chunks >= 1
    async with live_pool.acquire() as c:
        rows = await c.fetch(
            "SELECT content, source FROM memory WHERE claim_key LIKE 'doc:%' AND valid_until IS NULL")
    assert rows and all(r["source"] == "file_observation" for r in rows)
    joined = " ".join(r["content"] for r in rows)
    assert "secret@example.com" not in joined  # a secret in the doc is redacted before it's stored


@pytest.mark.db
async def test_reingest_unchanged_is_a_noop_and_changed_supersedes(live_pool: Any) -> None:
    path = _tmp_doc(live_pool, "d.txt", "version one content")
    svc = IngestService(live_pool, FakeModelProvider())
    first = await svc.ingest(path)
    assert first.status == "ok"

    again = await svc.ingest(path)
    assert again.status == "unchanged"  # identical hash → skipped

    Path(path).write_text("version two, quite different content now")
    changed = await svc.ingest(path)
    assert changed.status == "ok"
    async with live_pool.acquire() as c:
        current = await c.fetchval(
            "SELECT string_agg(content, ' ') FROM memory WHERE claim_key LIKE 'doc:%' "
            "AND valid_until IS NULL")
    assert "version two" in current and "version one" not in current  # old chunks retired


# ---- tool --------------------------------------------------------------------------------------
class _FakeIngest:
    def __init__(self) -> None:
        self.ingested: list[str] = []

    async def ingest(self, path: str) -> Any:
        self.ingested.append(path)
        return type("R", (), {"ok": True, "status": "ok", "chunks": 3})()


async def test_ingest_tool_confines_path_and_calls_service(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    sink = _FakeIngest()
    perms = PermissionsSettings(fs_read_roots=[str(tmp_path)], fs_write_roots=[str(tmp_path)])
    ctx = ToolContext(settings=Settings(permissions=perms), clock=SystemClock(), documents=sink)

    ok = await IngestDocument().run({"path": str(tmp_path / "a.txt")}, ctx)
    assert ok.ok and sink.ingested == [str((tmp_path / "a.txt").resolve())]

    denied = await IngestDocument().run({"path": "/root/secret.txt"}, ctx)  # outside the read root
    assert not denied.ok and "denied" in denied.display


def _tmp_doc(pool: Any, name: str, content: str) -> str:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="sali-ingest-"))
    p = d / name
    p.write_text(content)
    return str(p)
