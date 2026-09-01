-- 0038_digital_life.sql — Autonomous digital life, service ownership & long-lived action continuity.
-- Additive + minimal (§55: reuse, do not duplicate). The account/service entity, capability model,
-- side-effect/activity ledgers, experience, memory, graph, reviewer, cleanup, and natural clarification
-- already exist (migrations 0031-0037). This adds only what Prompt 9 genuinely needs:
--   1. generalise `external_entity` into the DigitalLifeObject (object_type, temporal validity,
--      relationships) — NOT a second entity system (§2/§17/§19).
--   2. a rich DIGITAL-ACTION lifecycle (planned → … → awaiting_external_state → verified → maintained)
--      that never confuses started/requested with completed/successful (§7/§10/§16).
--   3. OPEN OBLIGATIONS — the "unfinished business" invariant: a consequential action that isn't
--      conclusively finished leaves durable, resurfaceable state (§8/§9/§32).
--   4. COMMITMENTS — an enduring responsibility distinct from a task; one commitment spans many tasks
--      (§25/§26). Never collapsed into task_id.
-- No new memory/event/graph system. Credentials remain in the vault; nothing here holds a secret.

-- ── DigitalLifeObject: generalise the account/service entity (§2/§17/§19) ────────────────────────────
ALTER TABLE external_entity
  ADD COLUMN IF NOT EXISTS object_type    text NOT NULL DEFAULT 'account',   -- account|website|repo|domain|…
  ADD COLUMN IF NOT EXISTS last_verified  timestamptz,                       -- when external state was last confirmed (§17)
  ADD COLUMN IF NOT EXISTS relationships  jsonb NOT NULL DEFAULT '[]';       -- [{rel, target}] object↔object (§19)
CREATE INDEX IF NOT EXISTS ix_external_entity_type ON external_entity (object_type);

-- ── Digital action lifecycle (§7/§10) ───────────────────────────────────────────────────────────────
-- A service interaction, capability-level, mechanism-independent (§14). It links to a DigitalLifeObject
-- and carries the expected vs observed state so verification is explicit (§16). "started" is never
-- "completed"; "verification_required" must be resolved by evidence before "verified".
CREATE TABLE digital_action (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_id      uuid REFERENCES external_entity(id) ON DELETE SET NULL,
  task_id        uuid REFERENCES task(id) ON DELETE SET NULL,
  run_id         uuid,
  capability     text,                       -- the capability this action exercises
  intent         text NOT NULL,
  target         text,
  status         text NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned','ready','started','in_progress','awaiting_external_state',
                                   'verification_required','verified','maintained','failed','blocked',
                                   'authentication_required','permission_required','unsupported',
                                   'expired','unknown','cancelled')),
  expected_state text,
  observed_state text,
  evidence       jsonb NOT NULL DEFAULT '{}',
  error          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz);
CREATE INDEX ix_digital_action_object ON digital_action (object_id);
CREATE INDEX ix_digital_action_open ON digital_action (status)
  WHERE status NOT IN ('verified','maintained','failed','cancelled','unsupported');

-- ── Open obligations: the unfinished-business invariant (§8/§9/§32) ──────────────────────────────────
-- Something Sali started or accepted but has not conclusively finished. It is bounded (next_check +
-- priority, §28) so persistence never means "poll everything forever", and it resurfaces during
-- cognitive reconstruction. A resolved/cancelled obligation stays historically visible (§33).
CREATE TABLE obligation (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_action   uuid,                       -- soft ref to digital_action (that row may be archived)
  object_id       uuid,                       -- soft ref to the DigitalLifeObject
  task_id         uuid REFERENCES task(id) ON DELETE SET NULL,
  description     text NOT NULL,
  status          text NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','in_progress','resolved','cancelled','expired','blocked','unknown')),
  priority        int NOT NULL DEFAULT 5,
  blocking_reason text,
  next_action     text,
  evidence        jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_checked    timestamptz,
  next_check      timestamptz,
  resolved_at     timestamptz);
CREATE INDEX ix_obligation_open ON obligation (status, next_check)
  WHERE status IN ('open','in_progress','blocked');

-- ── Commitments: an enduring responsibility, distinct from a task (§25/§26) ─────────────────────────
-- "I'll send you the report." One commitment can produce many tasks/runs/interruptions, so it has its
-- own identity and survives conversation compaction. Never collapsed into task_id.
CREATE TABLE commitment (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      uuid,                          -- the originating task (soft ref; the commitment outlives it)
  description  text NOT NULL,
  status       text NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','in_progress','fulfilled','cancelled','blocked','expired','unknown')),
  deadline     timestamptz,
  next_action  text,
  evidence     jsonb NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  fulfilled_at timestamptz);
CREATE INDEX ix_commitment_open ON commitment (status) WHERE status IN ('open','in_progress','blocked');
