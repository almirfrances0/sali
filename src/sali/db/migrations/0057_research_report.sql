-- Downloadable research reports (Almir §): a research answer lands as a SHORT summary in the chat plus
-- a FULL markdown report saved to disk and served over the API — "summarise then for more i download the
-- file". Research is a conversation, not a task, so it needs its own small store + download route rather
-- than the task-artifact machinery (which is task-scoped).

CREATE TABLE IF NOT EXISTS sali.research_report (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid,
    run_id      uuid,
    title       text NOT NULL,
    query       text,
    path        text NOT NULL,          -- absolute path to the .md report on disk
    bytes       integer NOT NULL DEFAULT 0,
    words       integer NOT NULL DEFAULT 0,
    summary     text,                   -- the short chat summary (kept for reference / re-render)
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_research_report_created ON sali.research_report (created_at DESC);
