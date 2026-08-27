-- 0014_self_state.sql — Sali's persistent runtime self-model (§6/§7/§41/§84).
-- Operational self-awareness: a single durable row Sali continuously updates with what it is doing,
-- how it last fared, and how many turns it has served — so "who are you / what are you working on"
-- is answered from real state, not invented, and survives a reboot (§84). This holds only the
-- genuinely-NEW volatile fields; the rest of the self-view (identity, current task, uncertainties,
-- subsystem health) is COMPOSED at read time from the existing stores — never duplicated here.

CREATE TABLE sali_state (
  id               boolean PRIMARY KEY DEFAULT true CHECK (id),  -- singleton: only one row can exist
  mode             text NOT NULL DEFAULT 'idle',                 -- idle | working | learning
  current_focus    text,                                         -- what Sali is attending to now
  active_operation text,                                         -- the operation in flight, if any
  last_success     text,
  last_success_at  timestamptz,
  last_failure     text,
  last_failure_at  timestamptz,
  turn_count       bigint NOT NULL DEFAULT 0,
  updated_at       timestamptz NOT NULL DEFAULT now());

INSERT INTO sali_state (id) VALUES (true) ON CONFLICT DO NOTHING;
