-- 0035_experience_index.sql — Lifetime memory & experience (Prompt: lifetime memory §42/§43).
-- ADDITIVE and index-only. The lifetime-memory substrate REUSES the existing `memory` table (layered,
-- with provenance/confidence/evidence/temporal-validity/contradiction/pgvector — migrations 0001/0002/
-- 0010) rather than creating a parallel store (§43). A distilled experience is an EPISODIC memory whose
-- structured payload carries `kind='experience'` + its full task provenance. These expression indexes
-- keep experience lookups and "why do I believe this?" provenance queries off a full table scan (§42) —
-- no new columns, no new memory system.

-- Fast filter to current experiential memories (kind='experience') without scanning all of memory.
CREATE INDEX IF NOT EXISTS ix_memory_kind
  ON memory ((structured->>'kind'))
  WHERE valid_until IS NULL;

-- Fast provenance lookup: find the memory(ies) distilled from a given task (§11 "why do I know this").
CREATE INDEX IF NOT EXISTS ix_memory_task_provenance
  ON memory ((structured->>'task_id'))
  WHERE valid_until IS NULL AND structured ? 'task_id';
