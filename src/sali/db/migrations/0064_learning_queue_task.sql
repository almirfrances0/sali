-- Link a knowledge gap back to the work it blocked.
--
-- learning_queue could notice "this tool keeps failing" and research_pass could go learn the answer —
-- but the queue row had no way to name the task it came from, so "I have now learned the thing that
-- blocked me" and "therefore retry the blocked step" were divorced at the SCHEMA level. Sali could
-- learn the fix to a step he had already abandoned and never go back to it.
--
-- ON DELETE SET NULL: a deleted task must not delete the knowledge; the lesson outlives the job.
ALTER TABLE learning_queue
    ADD COLUMN IF NOT EXISTS task_id  uuid REFERENCES task(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS step_seq integer;

CREATE INDEX IF NOT EXISTS ix_learning_queue_task ON learning_queue (task_id)
    WHERE task_id IS NOT NULL;
