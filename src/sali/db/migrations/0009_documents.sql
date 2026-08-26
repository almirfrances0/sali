-- 0009_documents.sql — document ingestion (§44 document ingestion).
-- Tracks each ingested file by content hash so re-ingesting an UNCHANGED file is a no-op, and a
-- CHANGED file supersedes its old chunks. The chunk text itself lives in `memory` (FILE_OBSERVATION,
-- functional claim_key 'doc:<id>#<n>'), so Sali recalls and cites document content like any memory.

CREATE TABLE document (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  path         text NOT NULL UNIQUE,
  content_hash text NOT NULL,
  bytes        int NOT NULL,
  chunks       int NOT NULL DEFAULT 0,
  status       text NOT NULL DEFAULT 'ok',
  ingested_at  timestamptz NOT NULL DEFAULT now());
