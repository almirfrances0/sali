-- 0008_schedule.sql — scheduled / recurring work (§44 scheduled + background tasks).
-- A schedule fires a stored prompt as a full Sali turn at a cron/interval time. It's durable, so a
-- restart never loses a schedule; the daemon recomputes next_run_at each time it fires. Firing is
-- explicit opt-in autonomy (Almir created the schedule), balancing §23 "not fully autonomous".

CREATE TABLE schedule (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('cron', 'interval')),
  spec        text NOT NULL,                 -- "0 9 * * *" (cron) or "30m"/"2h"/"1d" (interval)
  prompt      text NOT NULL,                 -- what Sali should do when it fires (a turn prompt)
  enabled     boolean NOT NULL DEFAULT true,
  next_run_at timestamptz NOT NULL,
  last_run_at timestamptz,
  last_status text,                          -- 'ok' / 'error: …' from the most recent fire
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now());
-- The daemon only ever asks "what's due?" — index just the enabled rows by their next fire.
CREATE INDEX ix_schedule_due ON schedule (next_run_at) WHERE enabled;
