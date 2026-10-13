-- 0051_review_denorm_and_status.sql
-- Turn 8 of the Work-audit sequence: reviewer verdicts become visible task statuses.
--
-- Before: NEEDS_REWORK / BLOCKED verdicts only landed in `task_review`; the task's own
-- status stayed 'running' and observers (iOS, agenda, capsule) saw a phantom in-progress
-- task that wasn't actually moving. Audit called this out as Area 1 high.
--
-- Two new task statuses:
--   * `needs_changes` — a review returned NEEDS_REWORK; the task must be reworked.
--   * `blocked_by_review` — a review returned BLOCKED (permission required etc.); the
--     task is stuck until the block clears. Distinct from generic `blocked` (external).
--
-- Five denormalized last_review_* columns so a single-row SELECT can render the state.

ALTER TABLE sali.task DROP CONSTRAINT IF EXISTS task_status_check;
ALTER TABLE sali.task ADD CONSTRAINT task_status_check CHECK (
    status = ANY (ARRAY[
        'open', 'running', 'waiting', 'blocked', 'paused', 'done', 'failed',
        'abandoned', 'cancelled', 'superseded', 'waiting_for_user',
        -- Turn 8:
        'needs_changes', 'blocked_by_review'
    ])
);

ALTER TABLE sali.task
    ADD COLUMN IF NOT EXISTS last_review_id UUID,
    ADD COLUMN IF NOT EXISTS last_review_status TEXT,
    ADD COLUMN IF NOT EXISTS last_review_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_review_attempt INT,
    ADD COLUMN IF NOT EXISTS last_review_summary TEXT;

-- FK is soft — a review row could be archived independently. NULL when never reviewed.
COMMENT ON COLUMN sali.task.last_review_id IS
    'Turn 8: id of the most recent review row for this task (FK-shaped but not enforced).';
COMMENT ON COLUMN sali.task.last_review_status IS
    'Turn 8: passed / needs_rework / blocked / failed - denormalized from task_review.';
COMMENT ON COLUMN sali.task.last_review_at IS
    'Turn 8: when the most recent review row landed. NULL when never reviewed.';
COMMENT ON COLUMN sali.task.last_review_attempt IS
    'Turn 8: the review row''s attempt number - reveals the rework loop depth.';
COMMENT ON COLUMN sali.task.last_review_summary IS
    'Turn 8: short summary from the review (already summarized N passed, X failed).';
