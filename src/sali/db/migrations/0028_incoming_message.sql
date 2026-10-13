-- 0028_incoming_message.sql — the durable attention inbox (Prompt 1: interruptible attention).
-- A message Almir sends while Sali is busy must NEVER be lost. The coordinator's in-memory queue is
-- the transient runtime layer; this table is the durable one — messages survive a crash/restart, carry
-- a deterministic classification + priority, are claimed exactly once (FOR UPDATE SKIP LOCKED), and link
-- to the primary task they concern. Distinct from `message` (the conversation transcript): this is the
-- work queue of things-to-attend-to, not the chat log. Additive; forward-only.

CREATE TABLE incoming_message (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      uuid,                          -- the continuous conversation it belongs to
  content         text NOT NULL,
  origin          text NOT NULL DEFAULT 'cli',   -- cli | api | ios | scheduler | system
  priority        text NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  classification  text,                          -- attention category, set deterministically when routed
  related_task_id uuid REFERENCES task(id) ON DELETE SET NULL,  -- the primary task this concerns, if any
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'completed', 'deferred', 'cancelled')),
  run_id          uuid,                          -- the run that processed it (its own execution identity)
  dedup_key       text,                          -- optional idempotency key; a repeat enqueue is a no-op
  created_at      timestamptz NOT NULL DEFAULT now(),
  claimed_at      timestamptz,
  processed_at    timestamptz);

-- At most one queued message per idempotency key (multiple NULLs allowed — unkeyed messages never dedup).
CREATE UNIQUE INDEX ix_incoming_dedup ON incoming_message (dedup_key) WHERE dedup_key IS NOT NULL;

-- The pending queue: served highest-priority first, then FIFO within a priority. `priority_rank` mirrors
-- the Python Priority enum ordering (urgent > high > normal > low).
CREATE INDEX ix_incoming_pending ON incoming_message (created_at) WHERE status = 'pending';
CREATE INDEX ix_incoming_processing ON incoming_message (claimed_at) WHERE status = 'processing';

CREATE FUNCTION priority_rank(p text) RETURNS int LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE p WHEN 'urgent' THEN 3 WHEN 'high' THEN 2 WHEN 'normal' THEN 1 ELSE 0 END;
$$;
