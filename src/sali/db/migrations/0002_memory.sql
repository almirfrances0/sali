-- 0002_memory.sql — memory core (Phase 2).
-- Seven-layer memory in one table (discriminated by `layer`), per-layer policy in
-- `layer_policy`, per-observation evidence in `memory_evidence`, and non-persistent working
-- memory in the UNLOGGED `stm_observation`. Requires pgcrypto (digest), pg_trgm (gin),
-- and vector/hnsw — all created by the bootstrap script.

CREATE TABLE memory (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  layer          memory_layer NOT NULL,
  content        text NOT NULL,
  content_hash   bytea GENERATED ALWAYS AS (digest(content, 'sha256')) STORED,
  structured     jsonb NOT NULL DEFAULT '{}',
  source         memory_source NOT NULL,
  source_ref     uuid,
  source_detail  jsonb NOT NULL DEFAULT '{}',
  claim_key      text,
  functional     boolean NOT NULL DEFAULT false,
  confidence     real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  importance     real NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  reliability    real NOT NULL DEFAULT 0.5 CHECK (reliability BETWEEN 0 AND 1),
  evidence_count int  NOT NULL DEFAULT 1 CHECK (evidence_count >= 0),
  access_count   int  NOT NULL DEFAULT 0,
  last_accessed  timestamptz,
  freshness      freshness_policy NOT NULL DEFAULT 'slow',
  valid_from     timestamptz NOT NULL DEFAULT now(),
  valid_until    timestamptz,
  first_seen     timestamptz NOT NULL DEFAULT now(),
  last_seen      timestamptz NOT NULL DEFAULT now(),
  last_verified  timestamptz NOT NULL DEFAULT now(),
  superseded_by  uuid REFERENCES memory(id),
  needs_grounding boolean NOT NULL DEFAULT false,
  embedding      vector(768),
  embed_model    text,
  embed_status   text NOT NULL DEFAULT 'pending' CHECK (embed_status IN ('pending','done','error')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_until IS NULL OR valid_until >= valid_from));

-- one current identical restatement; one current value per functional claim
CREATE UNIQUE INDEX ux_memory_current ON memory (layer, content_hash)
  WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE UNIQUE INDEX ux_memory_claim ON memory (claim_key)
  WHERE valid_until IS NULL AND superseded_by IS NULL AND functional AND claim_key IS NOT NULL;
CREATE INDEX ix_memory_current ON memory (layer, importance DESC, confidence DESC)
  WHERE valid_until IS NULL;
CREATE INDEX ix_memory_embed_hnsw ON memory USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64) WHERE valid_until IS NULL AND embed_status = 'done';
CREATE INDEX ix_memory_trgm ON memory USING gin (content gin_trgm_ops);
CREATE INDEX ix_memory_valid ON memory (valid_from, valid_until);

CREATE TABLE memory_evidence (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id   uuid NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  source      memory_source NOT NULL,
  source_ref  uuid,
  observed_at timestamptz NOT NULL DEFAULT now(),
  confidence  real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  note        text,
  detail      jsonb NOT NULL DEFAULT '{}');
CREATE INDEX ix_evidence_memory ON memory_evidence (memory_id, observed_at DESC);

-- per-layer behavior as data (tunable without redeploy)
CREATE TABLE layer_policy (
  layer                memory_layer PRIMARY KEY,
  persistent           boolean NOT NULL,
  default_freshness    freshness_policy NOT NULL,
  base_importance      real NOT NULL,
  importance_half_life interval NOT NULL,
  promote_min_conf     real NOT NULL,
  promote_min_evidence smallint NOT NULL,
  functional_default   boolean NOT NULL DEFAULT false);
INSERT INTO layer_policy VALUES
  ('working',   false, 'realtime',  0.20, interval '10 minutes', 1.00, 99, false),
  ('episodic',  true,  'permanent', 0.30, interval '30 days',    0.35,  1, false),
  ('semantic',  true,  'daily',     0.50, interval '365 days',   0.55,  2, true),
  ('procedural',true,  'slow',      0.70, interval '180 days',   0.60,  1, true),
  ('preference',true,  'slow',      0.80, interval '730 days',   0.55,  2, true),
  ('system_env',true,  'weekly',    0.60, interval '90 days',    0.70,  1, true),
  ('identity',  true,  'permanent', 0.90, interval '3650 days',  0.70,  1, true);

-- working memory: non-persistent (UNLOGGED = no WAL, wiped on crash; durable truth is the event log)
CREATE UNLOGGED TABLE stm_observation (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  uuid,
  run_id      uuid,
  kind        text NOT NULL,
  content     text NOT NULL,
  structured  jsonb NOT NULL DEFAULT '{}',
  source      memory_source NOT NULL,
  source_ref  uuid,
  confidence  real NOT NULL DEFAULT 0.5,
  salience    real NOT NULL DEFAULT 0.5,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL DEFAULT now() + interval '2 hours');
CREATE INDEX ix_stm_session ON stm_observation (session_id, created_at);
CREATE INDEX ix_stm_expiry ON stm_observation (expires_at);
