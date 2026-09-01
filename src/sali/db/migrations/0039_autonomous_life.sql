-- 0039_autonomous_life.sql — Autonomous life, initiative & the digital world (Prompt 10).
-- Additive + minimal (§76/§77: no giant AutonomousAI, one source of truth per category, add only the
-- missing orchestration). Commitments, obligations, digital actions + reconciliation, external
-- identities, capability acquisition, experience/memory, the scheduler (wake), proactive messaging, the
-- 0-or-1 subagent, waiting_for_user, restart/compaction, and idempotency already exist (0031-0038).
-- This adds only the new durable primitives the autonomous-life loop orchestrates over:
--   goal (durable, hierarchical, origin-tagged) · routine (intentional recurring activity) ·
--   initiative (opportunity → candidate → … lifecycle, deterministically scored, deduped) ·
--   person (generic relationship state, evidence-grounded).

-- ── Goals: durable, hierarchical objectives beyond a single task (§5/§6) ─────────────────────────────
CREATE TABLE goal (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  objective        text NOT NULL,
  origin           text NOT NULL DEFAULT 'user'
                   CHECK (origin IN ('user','commitment','self_initiated','routine','environment',
                                     'learned','system')),
  priority         int NOT NULL DEFAULT 5,
  status           text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','active','blocked','completed','cancelled','deferred')),
  parent_goal      uuid REFERENCES goal(id) ON DELETE SET NULL,
  task_id          uuid,                       -- the current/originating task (soft ref; a goal spans tasks)
  commitment_id    uuid,                       -- soft ref to commitment
  deadline         timestamptz,
  progress         real NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
  constraints      jsonb NOT NULL DEFAULT '{}',
  success_conditions jsonb NOT NULL DEFAULT '[]',
  last_action      text,
  next_action      text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  completed_at     timestamptz);
CREATE INDEX ix_goal_open ON goal (status, priority) WHERE status IN ('open','active','blocked');
CREATE INDEX ix_goal_parent ON goal (parent_goal);

-- ── Routines: intentional recurring activities Sali understands (§8) ─────────────────────────────────
-- Timing reuses the existing scheduler semantics (interval / cron) but a routine is more than a cron
-- job: it has a purpose, conditions, and an outcome/failure history Sali reasons about.
CREATE TABLE routine (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL UNIQUE,
  purpose        text,
  schedule_kind  text NOT NULL DEFAULT 'interval',   -- 'interval' | 'cron'
  schedule_spec  text NOT NULL,
  conditions     jsonb NOT NULL DEFAULT '{}',
  enabled        boolean NOT NULL DEFAULT true,
  last_execution timestamptz,
  next_execution timestamptz,
  last_result    text,
  failure_count  int NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_routine_due ON routine (next_execution) WHERE enabled;

-- ── Initiatives: opportunity → candidate → … deterministically scored, deduped, backed off (§9-§12) ──
-- An observation is not an obligation; an opportunity is not permission. An initiative is durable, has a
-- lifecycle, and carries STRUCTURED scoring metadata (not chain-of-thought). Deduped by (source,
-- subject_ref) so the same opportunity is not re-created every cycle; attempts + next_attempt implement
-- backoff so a repeated failure doesn't burn the GPU forever (§40).
CREATE TABLE initiative (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source          text NOT NULL,              -- commitment|obligation|goal|routine|environment|capability_gap|self
  subject_ref     text NOT NULL,              -- the dedup key (e.g. the obligation id / capability name)
  title           text NOT NULL,
  status          text NOT NULL DEFAULT 'observed'
                  CHECK (status IN ('observed','candidate','evaluated','planned','ready','executing',
                                    'verifying','completed','dismissed','deferred','blocked','expired')),
  priority_score  real NOT NULL DEFAULT 0,
  urgency_score   real NOT NULL DEFAULT 0,
  risk_score      real NOT NULL DEFAULT 0,
  confidence      real NOT NULL DEFAULT 0.5,
  reason_codes    jsonb NOT NULL DEFAULT '[]',
  goal_id         uuid REFERENCES goal(id) ON DELETE SET NULL,
  commitment_id   uuid,
  attempts        int NOT NULL DEFAULT 0,
  next_attempt    timestamptz,
  evidence        jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz);
-- one live initiative per (source, subject_ref) — no duplicate opportunities (§14)
CREATE UNIQUE INDEX ux_initiative_live ON initiative (source, subject_ref)
  WHERE status NOT IN ('completed','dismissed','expired');
CREATE INDEX ix_initiative_open ON initiative (status, priority_score DESC)
  WHERE status NOT IN ('completed','dismissed','expired','blocked');

-- ── Person / relationship: generic, evidence-grounded social state (§23/§24) ─────────────────────────
-- Reuses the memory/identity architecture for facts; this table holds the durable relationship shell so
-- Sali does not meet the same person as a stranger each time. No sensitive attribute is inferred here.
CREATE TABLE person (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name              text NOT NULL UNIQUE,
  relationship_type text,                      -- colleague | client | friend | … (provenance-tagged)
  preferred_channel text,
  preferences       jsonb NOT NULL DEFAULT '{}',
  context           jsonb NOT NULL DEFAULT '{}',
  provenance        text NOT NULL DEFAULT 'observed'
                    CHECK (provenance IN ('explicit','observed','inferred','verified','uncertain')),
  confidence        real NOT NULL DEFAULT 0.5,
  last_interaction  timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now());
