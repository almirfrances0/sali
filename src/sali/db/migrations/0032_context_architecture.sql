-- 0032_context_architecture.sql — Prompt 6: decision ledger, task phases, context manifest.
-- Additive. The Task State Capsule is REGENERATED from existing durable state (task, task_step,
-- task_execution, task_artifact, task_review, task_research, task_skill, checkpoints) — this migration
-- adds only the genuinely new durable state: the decision ledger, task phases, and a reproducible
-- context manifest. No raw context is ever stored (§25/§50).

-- Decision ledger (§30): key task decisions that must survive compaction, with supersession so there
-- are never two contradictory ACTIVE decisions. Deterministic and durable, never left to model prose.
CREATE TABLE task_decision (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  run_id        uuid,
  decision      text NOT NULL,
  reason        text,
  source        text NOT NULL DEFAULT 'sali'
                CHECK (source IN ('user', 'sali', 'skill', 'research', 'reviewer')),
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
  superseded_by uuid,                 -- → task_decision.id (soft ref)
  created_at    timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_task_decision_active ON task_decision (task_id, status);

-- Task phases (§31/§32): very large tasks progress in phases; only the CURRENT phase plus prior phase
-- summaries normally enter context. Durable, structured.
CREATE TABLE task_phase (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  seq          int NOT NULL,
  name         text NOT NULL,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
  summary      text,                  -- deterministic phase-transition summary
  started_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (task_id, seq));
CREATE INDEX ix_task_phase_task ON task_phase (task_id, seq);

-- Context checkpoint / manifest (§23/§25): enough to REPRODUCE the working context after a restart —
-- the source IDs + a token estimate + the capsule version — never the raw context. Observability.
CREATE TABLE context_checkpoint (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         uuid REFERENCES task(id) ON DELETE CASCADE,
  run_id          uuid,
  step_seq        int,
  workspace       text,
  context_version int NOT NULL DEFAULT 1,
  source_ids      jsonb NOT NULL DEFAULT '{}',   -- {memories, research, executions, skills, decisions}
  token_estimate  int,
  reason          text,                          -- proactive | overflow | emergency | checkpoint
  created_at      timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_context_checkpoint_task ON context_checkpoint (task_id, created_at DESC);
