-- 0010_scopes.sql — memory scopes (§30/§31).
-- A memory can belong to a SCOPE — 'global' (applies everywhere), or a specific project / session —
-- so retrieval can surface the CURRENT project's knowledge heavily without flooding the context with
-- unrelated projects. Existing rows default to 'global', so nothing already stored changes behaviour.

ALTER TABLE memory ADD COLUMN scope text NOT NULL DEFAULT 'global';
CREATE INDEX ix_memory_scope ON memory (scope) WHERE valid_until IS NULL AND scope <> 'global';
