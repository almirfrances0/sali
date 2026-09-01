-- 0024_task_durability.sql — Durable task execution, heartbeat, and recovery.
-- A task must survive crashes, disconnection, and context compaction. The database is
-- authoritative — not the LLM's conversational context.

-- Heartbeat and recovery columns on task.
ALTER TABLE task ADD COLUMN last_heartbeat timestamptz;
ALTER TABLE task ADD COLUMN interrupted_at timestamptz;
ALTER TABLE task ADD COLUMN recovery_reason text;
ALTER TABLE task ADD COLUMN max_retries int NOT NULL DEFAULT 3;
ALTER TABLE task ADD COLUMN retry_count int NOT NULL DEFAULT 0;

-- Durable execution record: one row per tool call within a task, linking task steps to
-- actual tool executions. Survives crashes; used for idempotency checks and recovery.
CREATE TABLE task_execution (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  step_seq      int NOT NULL,
  tool_name     text NOT NULL,
  tool_args     jsonb NOT NULL DEFAULT '{}',
  execution_id  uuid,            -- → tool_execution.id (the actual run)
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'interrupted', 'skipped')),
  result_summary text,
  error         text,
  attempt       int NOT NULL DEFAULT 1,
  idempotent    boolean,         -- NULL = unknown, True = safe to retry, False = must verify
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  UNIQUE (task_id, step_seq, tool_name, attempt));
CREATE INDEX ix_task_execution_task ON task_execution (task_id, step_seq);
CREATE INDEX ix_task_execution_status ON task_execution (status) WHERE status IN ('running', 'interrupted');

-- Index for orphan detection: tasks that were running but whose heartbeat is stale.
CREATE INDEX ix_task_heartbeat ON task (last_heartbeat)
  WHERE status = 'running' AND is_primary;
