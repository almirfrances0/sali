-- Skill composer metadata on task_skill snapshots.
--
-- The SkillComposer now returns a plan of {primary, supporting, dependency} picks with a
-- machine-generated reason and (when the project could be inspected) a detected version. Prior
-- schema only stored (name, score). Persisting the composer metadata lets:
--
--   * the diagnostics UI show WHY each skill was picked (e.g. "detected in project (laravel@11.20)"
--     vs "relevance 3.5" vs "pulled in as a dependency of laravel"),
--   * proficiency (see sali.skills.proficiency) correlate skill outcomes with the KIND of role
--     the skill played (primary is a much stronger signal than a pulled-in dependency),
--   * re-composition on continuation know what shape the original selection took.
--
-- All three columns are nullable so existing rows continue to load unchanged (SkillStore.for_task
-- handles NULL as "supporting" / empty).

ALTER TABLE sali.task_skill
    ADD COLUMN IF NOT EXISTS kind             TEXT,
    ADD COLUMN IF NOT EXISTS reason           TEXT,
    ADD COLUMN IF NOT EXISTS detected_version TEXT;

-- Backfill: existing rows get 'supporting' with no reason. The old selector didn't emit reasons
-- and there's no way to reconstruct them; empty is honest.
UPDATE sali.task_skill SET kind = 'supporting' WHERE kind IS NULL;

-- No CHECK constraint on kind — new kinds may be added (e.g. 'contested', 'deprecated') without
-- a migration; the enum lives in Python (SkillComposer output) and stays flexible.
