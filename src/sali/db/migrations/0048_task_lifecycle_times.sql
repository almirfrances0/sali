-- 0048_task_lifecycle_times.sql — when a task actually STARTED and FINISHED (§13/§48).
--
--  recorded created_at, updated_at, last_progress_at and last_heartbeat, and none of those
-- answer the two questions a person asks: how long has this been running, and how long did it take.
-- created_at is when it was WRITTEN DOWN; a task can sit queued behind another for an hour before any
-- work begins, so created→completed silently counts the waiting as working.
--
-- Nullable on purpose. A task that has not started has no start time, and inventing one (defaulting to
-- created_at) would fabricate exactly the distinction this column exists to make. Existing rows stay
-- NULL rather than being back-filled with a guess: their real start times were never recorded and
-- pretending otherwise would write false history.
ALTER TABLE task ADD COLUMN IF NOT EXISTS started_at   timestamptz;
ALTER TABLE task ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE task ADD COLUMN IF NOT EXISTS deadline_at  timestamptz;

-- §48: temporal transitions must be logically valid. A task cannot finish before it was created, nor
-- start before it existed. NOT VALID so the constraint applies to new and updated rows without
-- rejecting any historical row whose timestamps predate it — history is preserved, never rewritten.
ALTER TABLE task DROP CONSTRAINT IF EXISTS task_times_ordered;
ALTER TABLE task ADD CONSTRAINT task_times_ordered CHECK (
      (started_at   IS NULL OR started_at   >= created_at)
  AND (completed_at IS NULL OR completed_at >= created_at)
  AND (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
) NOT VALID;

-- what was running yesterday is a range scan; without this it reads the table.
CREATE INDEX IF NOT EXISTS ix_task_started  ON task (started_at DESC) WHERE started_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_task_deadline ON task (deadline_at)     WHERE deadline_at IS NOT NULL;
