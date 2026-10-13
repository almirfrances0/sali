-- 0021_task_authority.sql — Active Task Authority: deterministic single-primary-task enforcement.
-- The user's newest request has higher authority than old memory, retrieval, or previous task state.
-- One task is PRIMARY ACTIVE at a time; old tasks become SUPERSEDED when a new independent task
-- starts. Additive columns + extended CHECK; no data loss.

-- Extend the task status vocabulary with 'superseded'.
ALTER TABLE task DROP CONSTRAINT task_status_check;
ALTER TABLE task ADD CONSTRAINT task_status_check CHECK (
  status IN ('open', 'running', 'waiting', 'blocked', 'paused', 'done', 'failed', 'abandoned', 'superseded'));

-- Track which task replaced this one (for audit / potential resume).
ALTER TABLE task ADD COLUMN superseded_by uuid REFERENCES task(id);

-- The primary-active flag: at most one task should have is_primary = true.
ALTER TABLE task ADD COLUMN is_primary boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX ix_task_primary ON task (is_primary) WHERE is_primary;

-- Track the previous active task on sali_state so the self-model and context always know.
ALTER TABLE sali_state ADD COLUMN active_task_id uuid REFERENCES task(id);
ALTER TABLE sali_state ADD COLUMN previous_task_id uuid REFERENCES task(id);
