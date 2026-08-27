-- 0015_learning_queue.sql — Sali's durable learning agenda (§45/§46).
-- Curiosity with a memory: when Sali notices something it doesn't understand — a new service it saw,
-- a procedure that keeps failing, a tool it hasn't learned — it records an item to investigate LATER,
-- rather than dropping everything to chase it (§46: bounded, never infinite autonomous experimentation).
-- The queue is worked off under a budget by the background learning pass. One pending item per
-- (kind, subject) so the same gap isn't queued twice; history is kept (resolved/dropped, never deleted).

CREATE TABLE learning_queue (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL,                       -- investigate | recurring_failure | unknown_tool | knowledge_gap
  subject     text NOT NULL,                       -- what to learn about
  reason      text,                                -- why it's worth learning
  priority    int NOT NULL DEFAULT 5,              -- 1 (highest) .. 9 (lowest)
  status      text NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'resolved', 'dropped')),
  outcome     text,                                -- what Sali learned, once resolved
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz);

CREATE UNIQUE INDEX ux_learning_queue_pending ON learning_queue (kind, subject) WHERE status = 'pending';
CREATE INDEX ix_learning_queue_open ON learning_queue (priority, created_at) WHERE status = 'pending';
