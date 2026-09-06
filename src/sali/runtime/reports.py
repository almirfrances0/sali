"""Downloadable research reports (Almir §).

A research answer is delivered as a short summary in the chat plus a FULL markdown report saved to disk
and served over the API — "he must summarise then for more i download the file". Research is a
conversation, not a task, so this is a small task-less store: it writes the report file under
sali-works/research/, records a row, and hands back the download URL the chat message points to.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "report").lower()).strip("-")
    return (s[:maxlen].strip("-") or "report")


class ResearchReportStore:
    """CRUD for downloadable research reports. Never raises on write — a report is a nicety on top of a
    reply that already reached Almir, so a failure here must not break the turn (callers still guard)."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def create(
        self, *, session_id: Any, run_id: Any, title: str, query: str,
        full_text: str, summary: str, root: str, when: str = "",
    ) -> dict[str, Any]:
        rid = uuid4()
        base = Path(root) / "research"
        base.mkdir(parents=True, exist_ok=True)
        filename = f"{slugify(title)}-{str(rid)[:8]}.md"
        path = base / filename
        header = f"# {title.strip() or 'Research'}\n\n"
        if query:
            header += f"> {query.strip()}\n\n"
        if when:
            header += f"_{when}_\n\n"
        body = header + "---\n\n" + (full_text or "").strip() + "\n"
        path.write_text(body, encoding="utf-8")
        nbytes = len(body.encode("utf-8"))
        words = len((full_text or "").split())
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sali.research_report "
                "(id, session_id, run_id, title, query, path, bytes, words, summary) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                rid, session_id, run_id, title[:200], (query or "")[:1000],
                str(path), nbytes, words, (summary or "")[:2000])
        return {
            "id": str(rid), "title": title, "filename": filename, "path": str(path),
            "bytes": nbytes, "words": words, "download_url": f"/api/v1/research/{rid}/download",
        }

    async def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, query, bytes, words, summary, created_at "
                "FROM sali.research_report ORDER BY created_at DESC LIMIT $1", limit)
        return [{
            "id": str(r["id"]), "title": r["title"], "query": r["query"],
            "bytes": r["bytes"], "words": r["words"], "summary": r["summary"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "download_url": f"/api/v1/research/{r['id']}/download",
        } for r in rows]

    async def get(self, report_id: UUID) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT id, title, path, bytes, words FROM sali.research_report WHERE id = $1", report_id)
        if r is None:
            return None
        return {"id": str(r["id"]), "title": r["title"], "path": r["path"],
                "bytes": r["bytes"], "words": r["words"]}


__all__ = ["ResearchReportStore", "slugify"]
