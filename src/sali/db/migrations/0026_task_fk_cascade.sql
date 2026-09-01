-- 0025_task_fk_cascade.sql — Fix FK constraints for task archival.
-- sali_state.active_task_id and task.superseded_by must allow the referenced task to be
-- deleted (archived) without blocking. ON DELETE SET NULL clears the reference automatically.
-- Without this, DELETE FROM task always fails for active/superseding tasks in production.

-- sali_state → task FKs: drop old, recreate with SET NULL
ALTER TABLE sali_state DROP CONSTRAINT IF EXISTS sali_state_active_task_id_fkey;
ALTER TABLE sali_state DROP CONSTRAINT IF EXISTS sali_state_previous_task_id_fkey;
ALTER TABLE sali_state ADD CONSTRAINT sali_state_active_task_id_fkey
  FOREIGN KEY (active_task_id) REFERENCES task(id) ON DELETE SET NULL;
ALTER TABLE sali_state ADD CONSTRAINT sali_state_previous_task_id_fkey
  FOREIGN KEY (previous_task_id) REFERENCES task(id) ON DELETE SET NULL;

-- task.superseded_by self-reference: drop old, recreate with SET NULL
ALTER TABLE task DROP CONSTRAINT IF EXISTS task_superseded_by_fkey;
ALTER TABLE task ADD CONSTRAINT task_superseded_by_fkey
  FOREIGN KEY (superseded_by) REFERENCES task(id) ON DELETE SET NULL;
