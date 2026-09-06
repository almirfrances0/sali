-- 0050_task_parent_task_id.sql
-- Turn 4 of the Work-audit sequence: §15 follow-up detection. When Almir says
-- "make the header smaller" after Sali finished a landing page, we match the message
-- against recently-completed tasks, and if a strong match is found, create a child
-- task that inherits the parent's workspace_root and allowed_write_roots.
--
-- The parent_task_id link is separate from superseded_by (which means "the OLD task was
-- replaced BY the NEW one"). A follow-up is the OPPOSITE relationship: the NEW child
-- continues from the PARENT (which stays done/archived). ON DELETE SET NULL so a future
-- retention cleanup that hard-removes really old archived rows doesn't cascade-delete
-- their children.

ALTER TABLE sali.task
    ADD COLUMN IF NOT EXISTS parent_task_id UUID
    REFERENCES sali.task(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS task_parent_task_id_idx
    ON sali.task (parent_task_id)
    WHERE parent_task_id IS NOT NULL;
