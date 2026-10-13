"""Document ingestion (spec §44): local files → chunked, redacted, citeable memories."""

from sali.ingest.models import IngestResult
from sali.ingest.service import IngestService

__all__ = ["IngestResult", "IngestService"]
