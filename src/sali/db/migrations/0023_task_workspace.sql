-- 0023_task_workspace.sql — Deterministic task workspace boundary.
-- A task that performs filesystem work declares a workspace_root: all writes must land inside it.
-- Artifact tracking records files that were actually created/modified by successful tool executions.
-- Additive; no data loss.

-- Workspace columns on task.
ALTER TABLE task ADD COLUMN workspace_root text;          -- resolved absolute path
ALTER TABLE task ADD COLUMN allowed_write_roots text[];   -- paths the task may write to
ALTER TABLE task ADD COLUMN workspace_mode text NOT NULL DEFAULT 'none'
  CHECK (workspace_mode IN ('none', 'explicit', 'inherited'));

-- Artifact tracking: only records from actual successful tool execution, never LLM claims.
CREATE TABLE task_artifact (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  artifact_path text NOT NULL,
  artifact_type text NOT NULL CHECK (artifact_type IN ('created', 'modified')),
  tool_name     text,
  created_at    timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_task_artifact_task ON task_artifact (task_id);
