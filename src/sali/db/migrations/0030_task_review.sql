-- 0030_task_review.sql — The reviewer gate's durable record (Prompt 4).
-- Sali may NOT mark a task 'done' merely because the model says so. A reviewer stage inspects durable
-- evidence (step verification, tool executions, artifacts on disk) and returns pass/needs_rework/blocked.
-- Every review ATTEMPT is durable and never overwritten, so Sali can see what it previously failed and
-- the rework loop is auditable. One logical task → many runs → many review attempts (same task_id).

CREATE TABLE task_review (
  review_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id              uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  run_id               uuid,                      -- the agent_run that triggered this review (nullable)
  attempt              int NOT NULL DEFAULT 1,     -- 1..N, increments per review of the same task
  status               text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'passed', 'failed', 'needs_rework', 'blocked')),
  reviewer_type        text NOT NULL DEFAULT 'deterministic',
  summary              text,                       -- concise operational summary (NEVER chain-of-thought)
  -- concise, machine-readable verification results — {requirement, status, evidence, kind} entries
  requirements_checked jsonb NOT NULL DEFAULT '[]',
  evidence_checked     jsonb NOT NULL DEFAULT '[]',
  failures             jsonb NOT NULL DEFAULT '[]',
  recommendations      jsonb NOT NULL DEFAULT '[]',
  passed_count         int NOT NULL DEFAULT 0,
  failed_count         int NOT NULL DEFAULT 0,
  unknown_count        int NOT NULL DEFAULT 0,
  started_at           timestamptz NOT NULL DEFAULT now(),
  completed_at         timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  -- one durable row per (task, attempt): a retried review is a NEW attempt, never an overwrite (§11)
  UNIQUE (task_id, attempt));

-- Latest-review lookups (the completion gate + the API + the continuation packet), and status/time scans.
CREATE INDEX ix_task_review_task    ON task_review (task_id, attempt DESC);
CREATE INDEX ix_task_review_status  ON task_review (status);
CREATE INDEX ix_task_review_created ON task_review (created_at);
