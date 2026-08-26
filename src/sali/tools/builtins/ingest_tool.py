"""ingest_document — Sali reads a file into its memory so it can recall and cite it later (§44).

The path is confined by the PathGuard (so fs_deny — credentials, off-limits projects — is honoured),
then handed to the ingestion service, which chunks + redacts + embeds it into FILE_OBSERVATION
memories. Read-only from the machine's view; it just adds to Sali's own memory.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.pathguard import PathViolation
from sali.tools.registry import ToolRegistry


class IngestDocument(Tool):
    name = "ingest_document"
    description = (
        "Read a document (text, markdown, code, csv/json, or a PDF) into your memory so you can "
        "recall and cite its content later. Give the file path. Re-ingesting an unchanged file does "
        "nothing; a changed file updates what you remember from it."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "The file to ingest."}},
        "required": ["path"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.READ, Capability.WRITE})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.documents is None:
            return ToolResult(ok=False, display="no ingest", error="ingestion isn't available")
        try:
            path = ctx.paths.check_read(args["path"])
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))

        result = await ctx.documents.ingest(str(path))
        if not result.ok:
            return ToolResult(ok=False, display=result.status,
                              error=result.detail or f"could not ingest ({result.status})")
        summary = (f"ingested {result.chunks} chunk(s) from {path.name}"
                   if result.status == "ok" else f"{path.name} already ingested, unchanged")
        return ToolResult(
            ok=True,
            output={"path": str(path), "status": result.status, "chunks": result.chunks},
            display=summary,
        )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(IngestDocument())
