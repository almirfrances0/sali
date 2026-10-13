-- 0041_capability_utilization.sql — Capability utilization, durable competence & purposeful acquisition
-- (Prompt 8B). Additive + minimal (§34: extend, do not create parallel abstractions). The capability
-- model, its lifecycle/confidence/freshness (degrade/restore/is_usable), gap analysis, reuse retrieval,
-- from-experience derivation, the resumable acquisition lifecycle, and accounts-as-resources
-- (external_entity) already exist (0037/0038). This adds only the invariant Prompt 8B is about — that an
-- acquired capability becomes a durable, discoverable, USABLE part of Sali's operational world:
--   • PURPOSE (why it was acquired, its current purpose) — §4/§5/§19/§21
--   • a resource link (a capability backed by an account/resource) — §6/§15
--   • USAGE history + last_used (a capability Sali has actually used, not merely read about) — §10/§12
-- No secrets here; credentials stay in the vault, resources in external_entity.

ALTER TABLE capability
  ADD COLUMN IF NOT EXISTS purpose     text,                 -- why acquired / current purpose (§4)
  ADD COLUMN IF NOT EXISTS resource_id uuid,                 -- soft ref to external_entity (the backing resource, §6)
  ADD COLUMN IF NOT EXISTS last_used   timestamptz,          -- when the capability was last actually used (§12)
  ADD COLUMN IF NOT EXISTS use_count   int NOT NULL DEFAULT 0;

-- Usage history — evidence that a capability is actually part of Sali's operational life, not a memory
-- of having once read about it (§12). Repeated successful use strengthens operational confidence (§27).
CREATE TABLE capability_usage (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capability_id uuid NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
  task_id       uuid,
  run_id        uuid,
  purpose       text,
  action        text,
  result        text,
  success       boolean NOT NULL DEFAULT true,
  verified      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_capability_usage_cap ON capability_usage (capability_id, created_at DESC);
