-- Persistent cache for web search + fetch.
--
-- Day-after-day background learning revisits the same topics, and without a cache every repeat costs a
-- real network round-trip: measured 5 identical queries back-to-back, all ~1.5s, all hitting upstream.
-- That is both slow and the single biggest way an unattended learner gets this host banned again — the
-- exact failure that made web search dead in the first place.
--
-- Disposable by design: it holds nothing Sali "knows", only what it recently fetched, so a factory reset
-- clearing it is correct and it can be truncated at any time without losing knowledge.
CREATE TABLE IF NOT EXISTS web_cache (
    key_hash     text PRIMARY KEY,             -- sha256 of (kind || target)
    kind         text NOT NULL CHECK (kind IN ('search', 'fetch')),
    target       text NOT NULL,                -- the query string, or the url
    payload      jsonb NOT NULL,               -- the tool output we can replay verbatim
    status       integer,
    fetched_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_web_cache_fetched ON web_cache (fetched_at DESC);
