-- 0016_machine_baseline.sql — the machine's learned "normal" (§18/§19).
-- Sali learns what its machine normally looks like so a genuine deviation stands out. The system
-- watcher's in-memory diff only knows the last snapshot — it forgets across a restart, so a change that
-- happened while Sali was off would be silently accepted as the new baseline. This table PERSISTS the
-- baseline (which listening ports are normally present, how consistently), so a port that appears while
-- Sali was down is still recognised as new, and something seen every day is understood as normal.

CREATE TABLE machine_baseline (
  kind         text NOT NULL,          -- 'port' (extensible to services/processes)
  item         text NOT NULL,          -- e.g. 'tcp:0.0.0.0:8080'
  observations bigint NOT NULL DEFAULT 1,
  first_seen   timestamptz NOT NULL DEFAULT now(),
  last_seen    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, item));
