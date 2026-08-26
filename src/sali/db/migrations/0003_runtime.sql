-- 0003_runtime.sql — the agent-run journal + tool lifecycle (Phase 1).
-- `agent_runs`/`run_events` are the per-turn FSM journal (write-ahead before every side
-- effect, for crash resume). `tool_execution` is the full plan→execute→observe→verify record
-- (engineering rule 13); `tool_audit` is its security-focused queryable projection.
-- These are DISTINCT from `task`/`task_step` (background jobs, a later phase).

CREATE TABLE agent_runs (
  run_id     uuid PRIMARY KEY,
  session_id uuid NOT NULL,
  user_input text NOT NULL,
  state      text NOT NULL,
  status     text NOT NULL,
  iteration  int NOT NULL DEFAULT 0,
  snapshot   jsonb NOT NULL DEFAULT '{}',
  started_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_agent_runs_running ON agent_runs (status) WHERE status = 'running';

CREATE TABLE run_events (
  event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id     uuid NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  seq        int NOT NULL,
  kind       text NOT NULL,
  payload    jsonb NOT NULL DEFAULT '{}',
  latency_ms int, tokens_in int, tokens_out int,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, seq));

CREATE TABLE tool_execution (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid NOT NULL,
  run_kind     text NOT NULL DEFAULT 'agent_run',
  tool_name    text NOT NULL,
  status       tool_status NOT NULL DEFAULT 'planned',
  danger_level smallint NOT NULL DEFAULT 0,
  approved_by  text,
  plan         jsonb NOT NULL,
  observed     jsonb,
  verification jsonb,
  success      boolean,
  error        text,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  duration_ms  int,
  CONSTRAINT ck_danger_approval CHECK (
    danger_level < 2 OR approved_by IS NOT NULL OR status IN ('planned','aborted')));
CREATE INDEX ix_tool_exec_run ON tool_execution (run_id, started_at);
CREATE INDEX ix_tool_exec_name ON tool_execution (tool_name, started_at DESC);

CREATE TABLE tool_audit (
  audit_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id        uuid NOT NULL,
  tool_name     text NOT NULL,
  risk_level    smallint NOT NULL,
  args_redacted jsonb NOT NULL,
  decision      text NOT NULL,
  ok            boolean,
  verified      boolean,
  duration_ms   int,
  created_at    timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_tool_audit ON tool_audit (tool_name, created_at);
CREATE INDEX ix_tool_audit_risk ON tool_audit (risk_level) WHERE risk_level >= 3;
