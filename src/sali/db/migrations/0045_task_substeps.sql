-- Sub-steps. Almir plans real work as "steps and sub-steps", and the Tasks screen needs to show that
-- shape; task_step was flat, so a plan could only ever be one level deep and the UI had nothing to
-- indent. `parent_seq` references another step's `seq` WITHIN THE SAME TASK (seq is unique per task),
-- which keeps the existing (task_id, seq) identity, depends_on arrays, and every existing query working
-- untouched — a NULL parent simply means a top-level step, which is what every existing row is.
ALTER TABLE task_step ADD COLUMN IF NOT EXISTS parent_seq integer;

-- A sub-step's parent must exist in the same task and cannot be itself; enforced in application code
-- rather than a composite FK so that step re-planning (which rewrites seq ranges) stays cheap.
CREATE INDEX IF NOT EXISTS ix_task_step_parent ON task_step (task_id, parent_seq)
    WHERE parent_seq IS NOT NULL;
