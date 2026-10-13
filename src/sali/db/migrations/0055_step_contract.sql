-- Step-discipline rebuild, Phase A: make each step a CONTRACT the engine can enforce, not a
-- bare description the model self-reports against.
--
-- Root problem (grounded on task 699b93f7 "ForgeDesk"): Sali marked steps 'done' that weren't on
-- disk, and did later steps' work while nominally on an earlier one. `advance_task` already enforces
-- IN-ORDER marking, but completion was TRUSTED (no check) and doing-ahead was UNPREVENTED (no scope).
--
-- Two additive, nullable columns close both:
--   definition_of_done — concrete, checkable completion criteria for THIS step. The engine verifies
--     it (e.g. named files exist) before accepting advance_task('done'); a step whose DoD is not met
--     stays open instead of being marked done on the model's word.
--   scope_excludes — what this step must NOT touch (files/areas belonging to later steps). Feeds the
--     scope guard: an out-of-scope write during this step is blocked/redirected, never mis-stamped as
--     this step's work.
--
-- Backward compatible: both nullable; existing rows and older code paths are unaffected (a NULL DoD
-- simply falls back to the prior trust-the-model behaviour for that step).

ALTER TABLE sali.task_step
    ADD COLUMN IF NOT EXISTS definition_of_done TEXT,
    ADD COLUMN IF NOT EXISTS scope_excludes     TEXT;

COMMENT ON COLUMN sali.task_step.definition_of_done IS
    'Concrete checkable completion criteria for this step; engine verifies before accepting done.';
COMMENT ON COLUMN sali.task_step.scope_excludes IS
    'What this step must NOT touch (later steps'' work); feeds the scope guard.';
