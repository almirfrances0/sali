-- 0031_workspace_skills_research.sql — Workspace authority, skills, and just-in-time research (Prompt 5).
-- Additive. The authoritative task WORKSPACE already lives on `task` (workspace_root/allowed_write_roots/
-- workspace_mode, migration 0023); this adds the durable state for the skills a task loaded, the web
-- research it performed, and evidence-aware learning candidates — so guidance, findings, and lessons
-- survive compaction, interruption, and restart from PostgreSQL rather than the model's memory.

-- Allow the deterministic auto-created workspace mode (§1) alongside the existing none/explicit/inherited.
ALTER TABLE task DROP CONSTRAINT IF EXISTS task_workspace_mode_check;
ALTER TABLE task ADD CONSTRAINT task_workspace_mode_check
  CHECK (workspace_mode IN ('none', 'explicit', 'inherited', 'auto'));

-- Skills selected for a task — with a CONTENT SNAPSHOT + hash, so continuation/recovery reproduces the
-- exact guidance the task started with even if the live .md changed halfway through (§10/§11).
CREATE TABLE task_skill (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  name         text NOT NULL,
  path         text NOT NULL,
  content_hash text NOT NULL,
  content      text NOT NULL,          -- the snapshot the task was selected with (durable reproducibility)
  score        real NOT NULL DEFAULT 0,
  selected_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, name));
CREATE INDEX ix_task_skill_task ON task_skill (task_id);

-- Just-in-time web research associated with a task (§16). Durable EVIDENCE, never automatically
-- permanent truth (§17) — the reviewer never treats it as completion proof (§22).
CREATE TABLE task_research (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  run_id        uuid,
  step_seq      int,
  query         text NOT NULL,
  source        text,                  -- primary source URL, when known
  summary       text NOT NULL,         -- bounded, extracted knowledge (not raw pages)
  confidence    real NOT NULL DEFAULT 0.5,
  content_hash  text,
  used          boolean NOT NULL DEFAULT false,   -- flipped when the finding is applied in the task
  status        text NOT NULL DEFAULT 'completed'
                CHECK (status IN ('completed', 'failed')),
  retrieved_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_task_research_task ON task_research (task_id, retrieved_at DESC);

-- Evidence-aware learning candidates (§19). A candidate becomes a durable lesson only once VERIFIED by
-- real execution/reviewer evidence; a failed experiment never auto-promotes (§18/§20). task_id is
-- SET NULL on task deletion so a promotable lesson outlives its task.
CREATE TABLE learning_candidate (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id            uuid REFERENCES task(id) ON DELETE SET NULL,
  run_id             uuid,
  lesson             text NOT NULL,
  source             text,                 -- evidence / research source
  research_id        uuid,                 -- → task_research.id (soft ref)
  verification_state text NOT NULL DEFAULT 'unverified'
                     CHECK (verification_state IN ('unverified', 'verified', 'failed')),
  confidence         real NOT NULL DEFAULT 0.5,
  promoted           boolean NOT NULL DEFAULT false,
  promoted_at        timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_learning_candidate_state ON learning_candidate (verification_state, promoted);
