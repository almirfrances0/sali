-- 0043_intent_and_resources.sql — Intent revocation, resource stewardship & self-preservation (Prompt 12).
-- Additive + minimal (§30: don't break stable systems). Task recovery already resurrects ONLY non-terminal
-- statuses (open/running/waiting/blocked/paused); 'abandoned'/'cancelled'/'superseded' are terminal and
-- already excluded, and _archive_and_cleanup archives (experience preserved) + removes the live row on a
-- terminal status — so a revoked task is already unresurrectable. The GPU inference lease + gate (temp/VRAM
-- via nvidia-smi) already guard the model boundary. This adds only what's missing:
--   • a durable REVOKED-INTENT tombstone — audit + revival detection, so recovery/initiative never treat
--     a historical task as current authorization (§4/§24/§25);
--   • a durable RESOURCE-INCIDENT ledger — host-endangering events become negative operational knowledge
--     that influences future planning (§21/§22/§23).

-- ── Intent tombstone (§4/§24): a revoked intention leaves durable evidence, never a live task ────────
CREATE TABLE revoked_intent (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid NOT NULL,             -- the (archived + removed) task's id
  objective     text,
  reason        text NOT NULL DEFAULT 'user_revoked',
  revoked_by    text NOT NULL DEFAULT 'user',
  superseded_by uuid,                       -- the new task, if the user later explicitly revives it (§25)
  revoked_at    timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_revoked_intent_task ON revoked_intent (task_id);
CREATE INDEX ix_revoked_intent_recent ON revoked_intent (revoked_at DESC);

-- ── Resource incidents (§21): host-endangering events as durable operational experience ─────────────
CREATE TABLE resource_incident (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL,               -- gpu_oom | vram_pressure | thermal | disk | ram | repeated_failure
  severity    text NOT NULL DEFAULT 'high'
              CHECK (severity IN ('elevated', 'high', 'critical', 'emergency')),
  workload    text,                         -- what caused it (task / model / context size)
  observed    jsonb NOT NULL DEFAULT '{}',  -- the measured resource values at the time (never chain-of-thought)
  mitigation  text,                         -- what reduced the pressure (for future reuse)
  resolved    boolean NOT NULL DEFAULT false,
  task_id     uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz);
CREATE INDEX ix_resource_incident_kind ON resource_incident (kind, created_at DESC);
CREATE INDEX ix_resource_incident_open ON resource_incident (created_at DESC) WHERE NOT resolved;
