-- 0049_task_archived_at.sql
-- Turn 1 of the Work-system audit sequence: the archive path was DELETEing task rows,
-- which is why sali.task is empty despite 25 historical task.created events - and why
-- §15 follow-up detection, §13 review history, and iOS history browsing all had nothing
-- to run against. From now on, the DB is the system of record for completed work; the
-- filesystem JSON snapshot remains as a durable export.
--
-- `archived_at` is set when a completed task's snapshot has been written to disk. It is
-- a metadata timestamp only - operational scans already filter by status, so archived
-- rows (status IN ('done','failed','abandoned','cancelled')) are naturally excluded from
-- "what's Sali working on?" queries.

ALTER TABLE sali.task
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS task_archived_at_idx
    ON sali.task (archived_at)
    WHERE archived_at IS NOT NULL;
