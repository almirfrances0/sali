-- 0047_schedule_timezone.sql — a cron schedule is CIVIL time, not UTC.
--
-- "Every day at 9am" means nine in the morning where the person lives. The spec was evaluated against
-- UTC, so on this system it fired at 12:00 for an owner in Africa/Dar_es_Salaam — and storing a
-- converted UTC hour instead would have been worse, because it silently slides an hour across every
-- DST transition. The zone is kept alongside the spec so the UTC instant can be RE-DERIVED at each
-- firing, which is what makes a 09:00 schedule stay at 09:00 all year.
--
-- Existing rows default to UTC: that is exactly how they have been behaving, so nothing changes
-- meaning underneath a schedule someone already created.
ALTER TABLE schedule ADD COLUMN IF NOT EXISTS timezone text NOT NULL DEFAULT 'UTC';

COMMENT ON COLUMN schedule.timezone IS
  'IANA zone the cron fields are civil time in. Ignored for interval kinds, which are durations.';
