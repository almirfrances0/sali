-- 0022_task_cancelled_status.sql — Add 'cancelled' to the task status vocabulary.
-- Semantics: CANCELLED = user/controller explicitly stopped the task.
-- ABANDONED = task left unfinished without explicit cancellation (different meaning).
-- Additive; no data loss. Existing 'abandoned' rows from explicit cancellation are left as-is
-- (they were the previous implementation's mapping); new cancellations will use 'cancelled'.

ALTER TABLE task DROP CONSTRAINT task_status_check;
ALTER TABLE task ADD CONSTRAINT task_status_check CHECK (
  status IN ('open', 'running', 'waiting', 'blocked', 'paused',
             'done', 'failed', 'abandoned', 'cancelled', 'superseded'));
