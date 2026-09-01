-- 0036_persistent_agency.sql — Persistent agency, completion discipline & the life loop.
-- Additive. The runtime already owns task/decision/phase/research/skill/reviewer/experience/memory
-- state; this adds only the SMALLEST set of new durable structures the life loop needs (§55): the
-- activity lifecycle (completion discipline), the side-effect ledger (no abandoned consequences), the
-- capability model (learned know-how, evidence-backed), and the durable workspace-cleanup ledger. The
-- unified "life state" is a DERIVED projection over these + existing stores (CognitiveState), not a new
-- source of truth (§3/§42). Nothing here weakens a core invariant (§60).

-- ── Activity: a unit of work WITHIN a task (§4/§6) ──────────────────────────────────────────────────
-- Completion discipline: every started activity must reach a terminal state — no permanent 'started'.
-- task_id is SET NULL on task cleanup so the activity history survives for audit/experience (§19/§27).
CREATE TABLE activity (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      uuid REFERENCES task(id) ON DELETE SET NULL,
  run_id       uuid,
  kind         text NOT NULL,               -- inspect | research | install | test | fix | review | …
  description  text,
  status       text NOT NULL DEFAULT 'started'
               CHECK (status IN ('started','completed','failed','blocked','cancelled','superseded',
                                 'waiting_for_user','deferred')),
  detail       jsonb NOT NULL DEFAULT '{}',
  error        text,
  started_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_activity_task ON activity (task_id, started_at);
CREATE INDEX ix_activity_open ON activity (status)
  WHERE status IN ('started','blocked','waiting_for_user','deferred');

-- ── Side-effect ledger: consequential actions with a lifecycle (§7/§8/§35/§53) ──────────────────────
-- No abandoned side effects: a consequential action is planned → attempted → succeeded/failed/reversed,
-- never silently left half-done. `execution_id` softly references task_execution where that already
-- captures the evidence (no duplication, §8). `idempotency_key` prevents repeating a done effect (§53).
CREATE TABLE side_effect (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         uuid REFERENCES task(id) ON DELETE SET NULL,
  run_id          uuid,
  activity_id     uuid REFERENCES activity(id) ON DELETE SET NULL,
  execution_id    uuid,                      -- soft ref to task_execution (that table is archived+cleaned)
  kind            text NOT NULL,             -- created|modified|deleted|downloaded|installed|sent|…
  target          text,
  before_state    text,
  after_state     text,
  status          text NOT NULL DEFAULT 'planned'
                  CHECK (status IN ('planned','attempted','succeeded','failed','reversed','unknown')),
  evidence        jsonb NOT NULL DEFAULT '{}',
  idempotency_key text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz);
-- one live effect per idempotency key — a succeeded/attempted effect is never re-planned (§53)
CREATE UNIQUE INDEX ux_side_effect_idem ON side_effect (idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status IN ('planned','attempted','succeeded');
CREATE INDEX ix_side_effect_task ON side_effect (task_id, status);
CREATE INDEX ix_side_effect_open ON side_effect (status) WHERE status IN ('planned','attempted');

-- ── Capability: what Sali has LEARNED to do, evidence-backed + environment-scoped (§12-16/§49) ───────
-- Distinct from a hardcoded TOOL: a capability is discovered, practiced, verified, and gains confidence
-- from actual evidence; it references the skills/procedures/experiences that support it. A capability
-- verified in one environment is NOT automatically universal (§16/§49) — scope is preserved.
CREATE TABLE capability (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL,
  status         text NOT NULL DEFAULT 'unknown'
                 CHECK (status IN ('unknown','researched','attempted','successful','verified',
                                   'available','degraded','deprecated')),
  scope          text NOT NULL DEFAULT 'environment'
                 CHECK (scope IN ('environment','project','global','user')),
  scope_ref      text,
  confidence     real NOT NULL DEFAULT 0.1 CHECK (confidence BETWEEN 0 AND 1),
  supported_by   jsonb NOT NULL DEFAULT '{}',   -- {skills, procedures, experiences, research, tools}
  evidence       jsonb NOT NULL DEFAULT '{}',
  times_succeeded int NOT NULL DEFAULT 0,
  times_failed    int NOT NULL DEFAULT 0,
  first_seen     timestamptz NOT NULL DEFAULT now(),
  last_verified  timestamptz,
  updated_at     timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_capability_scope ON capability (name, scope, coalesce(scope_ref, ''));
CREATE INDEX ix_capability_status ON capability (status);

-- ── Workspace-cleanup ledger: durable, resumable, honest (§24/§25/§26/§27) ──────────────────────────
-- An EPHEMERAL task workspace (mode 'auto', under <sali-works>/tasks/<id>/) is deleted after a verified
-- completion — but ONLY after experience extraction (§19). A USER-OWNED workspace is never auto-deleted
-- (§25). Cleanup state is durable (survives the task's deletion) so a failed cleanup is visible and
-- resumable, never silently pretended-done (§26).
CREATE TABLE workspace_cleanup (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id        uuid,                       -- soft ref (the task row is deleted on archive)
  workspace_root text NOT NULL,
  workspace_type text NOT NULL CHECK (workspace_type IN ('ephemeral','user_owned','external','none')),
  policy         text NOT NULL DEFAULT 'auto' CHECK (policy IN ('auto','keep')),
  status         text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','started','completed','failed','skipped')),
  reason         text,
  requested_at   timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz);
CREATE INDEX ix_workspace_cleanup_pending ON workspace_cleanup (status) WHERE status IN ('pending','failed');
CREATE UNIQUE INDEX ux_workspace_cleanup_ws ON workspace_cleanup (workspace_root, coalesce(task_id::text, ''));

-- ── Natural-language confirmations reuse task_question (§10/§11/§36) ─────────────────────────────────
-- A consequential-action confirmation is a question whose answer is interpreted SEMANTICALLY (never a
-- y/n gate). `kind` distinguishes it; `context` holds the proposed action so the answer can be applied.
ALTER TABLE task_question
  ADD COLUMN IF NOT EXISTS kind    text NOT NULL DEFAULT 'clarification',
  ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}';
