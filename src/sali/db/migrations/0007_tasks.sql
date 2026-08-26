-- 0007_tasks.sql — the Task Engine (§24): persistent, resumable multi-step tasks.
-- A task is a durable objective with ordered steps. Because it lives in the datastore (not in a
-- turn's memory), it survives process restarts: on the next turn Sali sees its open tasks in
-- context and picks up where it left off. Distinct from agent_runs (a single turn's FSM journal).

CREATE TABLE task (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id uuid,
  objective  text NOT NULL,
  status     text NOT NULL DEFAULT 'open'
             CHECK (status IN ('open', 'running', 'done', 'failed', 'abandoned')),
  result     text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now());
-- Only open/running tasks need resuming — index just those (they are few).
CREATE INDEX ix_task_open ON task (updated_at DESC) WHERE status IN ('open', 'running');

CREATE TABLE task_step (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id     uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  seq         int NOT NULL,
  description text NOT NULL,
  status      text NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
  note        text,
  started_at  timestamptz,
  finished_at timestamptz,
  UNIQUE (task_id, seq));
CREATE INDEX ix_task_step ON task_step (task_id, seq);
