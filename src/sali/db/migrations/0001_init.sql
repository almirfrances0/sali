-- 0001_init.sql — Sali schema spine.
-- Establishes the shared enum vocabulary, the evidence-priority function, the freshness
-- lookup, and the append-only event log + conversation tables. Memory/graph/twin/learning
-- tables arrive in later phases (they reuse everything defined here).
-- Runs with search_path = sali, public (set by the migration runner). No extensions are
-- created here; the privileged bootstrap script installs pgcrypto/pg_trgm/vector.

-- ---- enum vocabulary (mirrors sali.core.enums) --------------------------------------
CREATE TYPE memory_source AS ENUM (
  'user_explicit','conversation','system_observation','file_observation',
  'tool_result','external_source','inference','procedure_execution');

CREATE TYPE memory_layer AS ENUM (
  'working','episodic','semantic','procedural','preference','system_env','identity');

CREATE TYPE freshness_policy AS ENUM (
  'realtime','fast','hourly','daily','weekly','slow','permanent');

CREATE TYPE task_status AS ENUM (
  'pending','running','paused','blocked','completed','failed','cancelled');

CREATE TYPE step_status AS ENUM (
  'pending','running','succeeded','failed','skipped');

CREATE TYPE tool_status AS ENUM (
  'planned','executing','observed','verified_success','verified_failure','aborted');

CREATE TYPE contradiction_status AS ENUM ('open','resolved','unresolvable');

-- ---- evidence priority as data (engineering rule 6) ---------------------------------
CREATE FUNCTION source_priority(s memory_source) RETURNS smallint
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE s
    WHEN 'system_observation'  THEN 100
    WHEN 'file_observation'    THEN 100
    WHEN 'user_explicit'       THEN 80
    WHEN 'tool_result'         THEN 60
    WHEN 'procedure_execution' THEN 60
    WHEN 'external_source'      THEN 40
    WHEN 'conversation'        THEN 30
    WHEN 'inference'           THEN 20
    ELSE 0
  END;
$$;

-- ---- freshness thresholds (tunable without redeploy) --------------------------------
CREATE TABLE freshness_rule (
  policy   freshness_policy PRIMARY KEY,
  max_age  interval NOT NULL);
INSERT INTO freshness_rule VALUES
  ('realtime', interval '0'),
  ('fast',     interval '1 minute'),
  ('hourly',   interval '1 hour'),
  ('daily',    interval '1 day'),
  ('weekly',   interval '7 days'),
  ('slow',     interval '90 days'),
  ('permanent',interval '1000 years');

-- ---- append-only event spine (engineering rule 15) ----------------------------------
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS
  $$ BEGIN RAISE EXCEPTION 'append-only: % on % is not permitted', TG_OP, TG_TABLE_NAME; END $$;

CREATE TABLE event (
  seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  id          uuid NOT NULL DEFAULT gen_random_uuid(),
  event_type  text NOT NULL,
  subject_type text,
  subject_id  uuid,
  actor       text NOT NULL DEFAULT 'sali',
  payload     jsonb NOT NULL DEFAULT '{}',
  latency_ms  int,
  tokens_in   int,
  tokens_out  int,
  created_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_event_created_brin ON event USING brin (created_at);
CREATE INDEX ix_event_subject ON event (subject_type, subject_id, seq);
CREATE INDEX ix_event_type ON event (event_type, seq);
CREATE TRIGGER trg_event_immutable BEFORE UPDATE OR DELETE ON event
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---- conversation + messages --------------------------------------------------------
CREATE TABLE conversation (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel     text NOT NULL DEFAULT 'terminal',
  title       text,
  started_at  timestamptz NOT NULL DEFAULT now(),
  last_active timestamptz NOT NULL DEFAULT now(),
  metadata    jsonb NOT NULL DEFAULT '{}');

CREATE TABLE message (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
  seq             int NOT NULL,
  role            text NOT NULL,
  content         text NOT NULL,
  token_count     int,
  model           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, seq));
CREATE INDEX ix_message_conversation ON message (conversation_id, seq);
