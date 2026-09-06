-- Memory browsing at lifetime scale.
--
-- The management surface has to page through memory by recency with arbitrary filters. Measured on a
-- scratch database seeded with 100,009 real rows: without an ordering index the default browse and every
-- keyset page fell to a parallel seq scan plus a top-N heapsort at 13-16 ms, growing with the table. A
-- composite (layer, source, created_at DESC, id DESC) did NOT fix it — PostgreSQL 18 skip-scan fires, but
-- it cannot serve the global sort, so an unfiltered browse still seq-scanned. The ORDERING is what
-- matters; the filters are cheap once the scan is index-ordered.
--
-- With this index the same queries run 0.037-0.27 ms and, crucially, stay flat as the table grows,
-- because keyset pagination turns "page 900" into an index seek rather than an offset walk.
CREATE INDEX IF NOT EXISTS ix_memory_recent ON memory (created_at DESC, id DESC);

-- Provenance filtering ("everything Almir told me", "everything I inferred") and the layer facets the
-- overview screen counts. Skip-scan makes the leading column optional, so one index serves both.
CREATE INDEX IF NOT EXISTS ix_memory_layer_source ON memory (layer, source, created_at DESC);

-- The two review queues the app surfaces: beliefs Sali has flagged as unverified, and claims that were
-- superseded (so a correction can be traced back to what it replaced). Partial, so they stay small.
CREATE INDEX IF NOT EXISTS ix_memory_ungrounded ON memory (importance DESC, last_verified)
    WHERE needs_grounding AND valid_until IS NULL;
CREATE INDEX IF NOT EXISTS ix_memory_superseded ON memory (superseded_by)
    WHERE superseded_by IS NOT NULL;
