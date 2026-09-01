-- 0027_task_progress_tracking.sql — Deterministic progress tracking.
-- Distinguishes heartbeat (process alive) from progress (meaningful work done).
-- The watchdog uses last_progress_at to detect potentially-stuck tasks.

-- Last meaningful progress timestamp — updated on step advance, tool success,
-- artifact creation, checkpoint. NOT updated on heartbeat or LLM prose.
ALTER TABLE task ADD COLUMN last_progress_at timestamptz;
ALTER TABLE task ADD COLUMN last_progress_type text;
-- What tool was actively executing when last seen (for watchdog context)
ALTER TABLE task ADD COLUMN active_tool_name text;
-- Watchdog diagnostic — does NOT change task.status execution semantics
ALTER TABLE task ADD COLUMN health_status text NOT NULL DEFAULT 'healthy'
  CHECK (health_status IN ('healthy', 'active_tool', 'potentially_stuck', 'orphaned'));

-- Index for watchdog queries: find active tasks that might be stuck
CREATE INDEX ix_task_health ON task (health_status, last_progress_at)
  WHERE status = 'running' AND is_primary;
