-- 0020_task_lifecycle.sql — richer durable task lifecycle (§3-6): waiting/blocked/paused states, a
-- per-step dependency DAG, and a per-step checkpoint so a long step resumes MID-step after a crash.
-- Additive / forward-only. We EXTEND the active text+CHECK vocabulary rather than switch to the dormant
-- PG enums (task_status/step_status from 0001): the live values 'open'/'done'/'abandoned' are the model's
-- advance_task/finish_task tool interface and the whole task test-suite, and the enums' 'completed'/
-- 'succeeded'/'cancelled' would force a breaking rename for no functional gain. One vocabulary, extended.

ALTER TABLE task DROP CONSTRAINT task_status_check;
ALTER TABLE task ADD CONSTRAINT task_status_check CHECK (
  status IN ('open', 'running', 'waiting', 'blocked', 'paused', 'done', 'failed', 'abandoned'));

ALTER TABLE task_step DROP CONSTRAINT task_step_status_check;
ALTER TABLE task_step ADD CONSTRAINT task_step_status_check CHECK (
  status IN ('pending', 'running', 'waiting', 'blocked', 'done', 'failed', 'skipped'));

ALTER TABLE task_step
  ADD COLUMN depends_on int[]  NOT NULL DEFAULT '{}',  -- step seqs this step waits on (a per-step DAG)
  ADD COLUMN checkpoint jsonb;                         -- in-step progress → resume mid-step, not from scratch
