-- 0034_adaptive_learning.sql — Adaptive intelligence, daily learning & behavior evolution (Prompt 7).
-- Additive. Sali improves through EVIDENCE-BACKED learning: experience → observation → evidence →
-- candidate → evaluation → promotion → future behavior. Nothing here lets learning override a core
-- invariant (workspace/lease/reviewer/single-primary) — learning influences DECISIONS, never the kernel.
--
-- The Prompt-5 `learning_candidate` table already exists (0031). This EXTENDS it with the evidence
-- hierarchy, scope, and observation counters the adaptive loop needs, and adds the durable structures
-- for behavioral learning, knowledge contradictions, skill-improvement proposals, and the idempotent
-- daily-consolidation ledger.

-- ── Evidence-aware learning candidates (extend, don't replace) ─────────────────────────────────────
-- scope (§8, mandatory): where a lesson applies. source_type: what evidence produced it.
-- evidence_level (§4, 0-5): 0 model-assertion → 5 reviewer-verified. times_* : the observation history
-- confidence is DERIVED from. content_hash + scope_ref: deterministic dedup so the DB stays small (§13).
-- claim_key/claim_value: an optional structured claim, so a genuine contradiction is detectable (§11).
ALTER TABLE learning_candidate
  ADD COLUMN IF NOT EXISTS scope           text NOT NULL DEFAULT 'task',
  ADD COLUMN IF NOT EXISTS scope_ref       text,
  ADD COLUMN IF NOT EXISTS source_type     text,
  ADD COLUMN IF NOT EXISTS evidence_level  smallint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS times_observed  int NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS times_successful int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS times_failed    int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS content_hash    text,
  ADD COLUMN IF NOT EXISTS claim_key       text,
  ADD COLUMN IF NOT EXISTS claim_value     text,
  ADD COLUMN IF NOT EXISTS superseded_by   uuid REFERENCES learning_candidate(id),
  ADD COLUMN IF NOT EXISTS updated_at      timestamptz NOT NULL DEFAULT now();

ALTER TABLE learning_candidate ADD CONSTRAINT learning_candidate_scope_check
  CHECK (scope IN ('task', 'project', 'skill', 'environment', 'user', 'global'));

-- The knowledge lifecycle (§12). Widen the state set; the old 'unverified'/'verified'/'failed' remain
-- valid so existing rows/callers keep working.
ALTER TABLE learning_candidate DROP CONSTRAINT IF EXISTS learning_candidate_verification_state_check;
ALTER TABLE learning_candidate ADD CONSTRAINT learning_candidate_verification_state_check
  CHECK (verification_state IN (
    'unverified', 'observed', 'attempted', 'supported', 'verified', 'contradicted',
    'promoted', 'rejected', 'superseded', 'failed', 'unknown'));

-- Dedup (§13): one live candidate per (scope, scope_ref, content_hash). A superseded/rejected row is
-- excluded so a lesson can be re-observed after being retired.
CREATE UNIQUE INDEX IF NOT EXISTS ux_learning_candidate_dedup
  ON learning_candidate (scope, coalesce(scope_ref, ''), content_hash)
  WHERE content_hash IS NOT NULL AND verification_state NOT IN ('superseded', 'rejected');
CREATE INDEX IF NOT EXISTS ix_learning_candidate_scope ON learning_candidate (scope, scope_ref);
CREATE INDEX IF NOT EXISTS ix_learning_candidate_claim ON learning_candidate (claim_key)
  WHERE claim_key IS NOT NULL;

-- ── Behavioral learning (§6/§7/§16/§19): a proposal, never an automatic mutation ────────────────────
-- User feedback and repeated patterns become behavior CANDIDATES; a candidate affects future planning
-- only once ACCEPTED. Core-behavior changes require user approval (§42); scope is preserved so a
-- project preference never silently becomes global (§8).
CREATE TABLE behavior_proposal (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope             text NOT NULL DEFAULT 'user'
                    CHECK (scope IN ('task', 'project', 'skill', 'environment', 'user', 'global')),
  scope_ref         text,
  trigger           text NOT NULL,               -- when this behavior applies
  current_behavior  text,                        -- what Sali does now (may be unknown)
  proposed_behavior text NOT NULL,               -- what the evidence suggests instead
  reason            text,
  source_type       text,                        -- 'user_feedback' | 'pattern' | 'recovery' | ...
  evidence          jsonb NOT NULL DEFAULT '{}',
  times_observed    int NOT NULL DEFAULT 1,
  confidence        real NOT NULL DEFAULT 0.3,
  status            text NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate', 'testing', 'accepted', 'rejected', 'superseded')),
  content_hash      text,
  superseded_by     uuid REFERENCES behavior_proposal(id),
  decided_by        text,
  decided_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_behavior_proposal_dedup
  ON behavior_proposal (scope, coalesce(scope_ref, ''), content_hash)
  WHERE content_hash IS NOT NULL AND status NOT IN ('rejected', 'superseded');
CREATE INDEX ix_behavior_proposal_status ON behavior_proposal (status);
CREATE INDEX ix_behavior_proposal_scope ON behavior_proposal (scope, scope_ref);

-- ── Knowledge contradictions (§11): record, never silently overwrite ────────────────────────────────
-- Distinct from the graph-level `contradiction` (memory/edge, 0004) — this is a learning-knowledge
-- conflict between candidates/promoted lessons under the same claim, resolved by evidence or flagged.
CREATE TABLE learning_contradiction (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope        text NOT NULL DEFAULT 'global',
  scope_ref    text,
  claim_key    text,
  old_id       uuid,                             -- learning_candidate.id of the standing claim
  new_id       uuid,                             -- learning_candidate.id of the conflicting claim
  old_claim    text NOT NULL,
  new_claim    text NOT NULL,
  status       text NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'resolved', 'needs_review')),
  resolution   text,
  detail       jsonb NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  resolved_at  timestamptz);
CREATE INDEX ix_learning_contradiction_open ON learning_contradiction (status) WHERE status = 'open';

-- ── Skill-improvement proposals (§14/§15): reviewable, never a blind rewrite ─────────────────────────
-- Repeated success with an approach diverging from a skill's current guidance becomes a PROPOSAL. The
-- live .md is never auto-edited; existing task_skill snapshots keep old tasks reproducible.
CREATE TABLE skill_proposal (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_name       text NOT NULL,
  current_guidance text,
  proposed_change  text NOT NULL,
  reason           text,
  evidence         jsonb NOT NULL DEFAULT '{}',
  times_successful int NOT NULL DEFAULT 0,
  times_failed     int NOT NULL DEFAULT 0,
  confidence       real NOT NULL DEFAULT 0.3,
  status           text NOT NULL DEFAULT 'candidate'
                   CHECK (status IN ('candidate', 'accepted', 'rejected', 'superseded')),
  content_hash     text,
  decided_by       text,
  decided_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_skill_proposal_dedup ON skill_proposal (skill_name, content_hash)
  WHERE content_hash IS NOT NULL AND status NOT IN ('rejected', 'superseded');
CREATE INDEX ix_skill_proposal_status ON skill_proposal (status);

-- ── Daily-consolidation ledger (§9/§26): idempotent, one cycle per day, bounded catch-up ────────────
-- The UNIQUE(ran_on) is the idempotency guarantee: a re-run for the same day is a no-op; a restart
-- cannot duplicate a cycle; missed days are found deterministically and caught up (bounded).
CREATE TABLE consolidation_run (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ran_on       date NOT NULL UNIQUE,
  status       text NOT NULL DEFAULT 'running'
               CHECK (status IN ('running', 'completed', 'failed')),
  summary      jsonb NOT NULL DEFAULT '{}',
  started_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz);
