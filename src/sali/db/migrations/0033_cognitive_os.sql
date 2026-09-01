-- 0033_cognitive_os.sql — Cognitive OS: one bounded subagent + user-clarification state.
-- Additive. The CognitiveState itself is DERIVED (no table); this adds only the genuinely new durable
-- state: a delegation record for the single optional subagent (§16), and a durable user-clarification
-- question so a task can legitimately pause and ask, then resume the SAME task (§44).

CREATE TABLE delegation (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_task_id uuid REFERENCES task(id) ON DELETE CASCADE,
  parent_run_id  uuid,
  session_id     uuid,                  -- the subagent's own bounded session
  objective      text NOT NULL,
  workspace      text,
  status         text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
  result         text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz);
-- At most ONE running delegation per parent task (§15/§16/§17) — the runtime enforces 0-or-1.
CREATE UNIQUE INDEX ux_delegation_one_running ON delegation (parent_task_id) WHERE status = 'running';
CREATE INDEX ix_delegation_parent ON delegation (parent_task_id, status);

-- User-clarification (§44): a task may stop and ask the user, then resume the same task on their reply.
CREATE TABLE task_question (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id     uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  run_id      uuid,
  question    text NOT NULL,
  status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'cancelled')),
  answer      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  answered_at timestamptz);
CREATE INDEX ix_task_question_task ON task_question (task_id, status);

-- Allow the durable 'waiting_for_user' task status (a legitimate pause, not a failure — §44).
ALTER TABLE task DROP CONSTRAINT IF EXISTS task_status_check;
ALTER TABLE task ADD CONSTRAINT task_status_check
  CHECK (status IN ('open', 'running', 'waiting', 'blocked', 'paused', 'done', 'failed',
                    'abandoned', 'cancelled', 'superseded', 'waiting_for_user'));
