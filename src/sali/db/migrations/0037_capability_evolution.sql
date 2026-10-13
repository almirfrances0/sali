-- 0037_capability_evolution.sql — Agency, capability evolution & real-world action (Prompt 8).
-- Additive + minimal (§72: smallest production-grade, no giant framework). The capability model,
-- experience, commitments-as-side-effects, cleanup, reviewer, and natural clarification already exist
-- (migrations 0034-0036). This adds only what Prompt 8 genuinely needs on top:
--   1. capability DEPENDENCIES (composition / a capability graph, §8/§9)
--   2. a durable capability-ACQUISITION lifecycle (the gap → research → acquire → verify loop, §5/§10)
--   3. EXTERNAL ENTITIES / accounts as durable entities distinct from memory (§11/§12) — NEVER secrets.
-- No new memory system, no parallel capability store. Credentials stay in the existing secret vault.

-- ── Capability dependencies (§8/§9) ─────────────────────────────────────────────────────────────────
-- A capability can require other capabilities ("deploy website" needs shell + git + browser + …). The
-- list is capability NAMES; gap analysis resolves them against what is actually verified/available.
ALTER TABLE capability
  ADD COLUMN IF NOT EXISTS depends_on jsonb NOT NULL DEFAULT '[]';

-- ── Capability acquisition lifecycle (§5/§10/§68.10-18) ─────────────────────────────────────────────
-- When Sali finds he lacks a capability, the attempt to acquire it is DURABLE state: it survives
-- interruption, compaction, and restart, and is resumable — never restarted from zero. A failed
-- acquisition stays resumable and never becomes a claimed capability (§3/§42).
CREATE TABLE capability_acquisition (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capability_name text NOT NULL,
  task_id         uuid REFERENCES task(id) ON DELETE SET NULL,
  run_id          uuid,
  scope           text NOT NULL DEFAULT 'environment',
  scope_ref       text,
  status          text NOT NULL DEFAULT 'gap_identified'
                  CHECK (status IN ('gap_identified','researching','acquiring','verifying','acquired',
                                    'failed','blocked')),
  gap             jsonb NOT NULL DEFAULT '{}',   -- {required:[…], missing:[…]}
  notes           text,
  error           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz);
-- one live acquisition per capability+scope (re-identifying the same gap resumes it, never duplicates)
CREATE UNIQUE INDEX ux_capability_acquisition_live
  ON capability_acquisition (capability_name, scope, coalesce(scope_ref, ''))
  WHERE status NOT IN ('acquired','failed');
CREATE INDEX ix_capability_acquisition_open ON capability_acquisition (status)
  WHERE status IN ('gap_identified','researching','acquiring','verifying','blocked');

-- ── External entities / accounts (§11/§12/§34/§35/§68.31-33) ────────────────────────────────────────
-- An external identity Sali interacts with (an account, a service, a repo). It is a durable ENTITY, not
-- a memory: it has a lifecycle from discovered → … → active/closed and a verification state, so a
-- half-created account is never silently abandoned (§13). Credentials are NEVER stored here — only a
-- boolean saying whether a credential is configured in the vault (§11/§65).
CREATE TABLE external_entity (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  service             text NOT NULL,             -- 'email' | 'github' | 'staging-server' | … (not hardcoded)
  ref                 text,                      -- a non-secret identity reference (username, url, id)
  purpose             text,
  task_id             uuid REFERENCES task(id) ON DELETE SET NULL,
  authority           text,                      -- who authorised it (e.g. 'user')
  status              text NOT NULL DEFAULT 'discovered'
                      CHECK (status IN ('discovered','planned','created','verification_pending',
                                        'verified','configured','active','degraded','suspended','closed')),
  verification_state  text NOT NULL DEFAULT 'unverified'
                      CHECK (verification_state IN ('unverified','pending','verified','failed')),
  credential_configured boolean NOT NULL DEFAULT false,   -- the secret lives in the vault, never here
  last_observed       jsonb NOT NULL DEFAULT '{}',
  notes               text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_external_entity ON external_entity (service, coalesce(ref, ''));
CREATE INDEX ix_external_entity_open ON external_entity (status)
  WHERE status NOT IN ('active','closed');
