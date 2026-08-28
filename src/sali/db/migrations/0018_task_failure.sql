-- 0018_task_failure.sql — durable per-step failure classification + verified-effect linkage (§8/§9).
-- Additive columns only; NO lifecycle/status change (that is a later increment). When a step fails, the
-- store records how many times it has been attempted, the last error text, and a FailureClass — mirroring
-- sali.core.errors.FailureClass — so the retry driver can tell TRANSIENT/RECOVERABLE (auto-retry) from
-- PERMISSION/FATAL (escalate). `verified`/`verified_by` link a step to the tool_execution that VERIFIED
-- its effect, separating "performed" from "verified" (§8). Forward-only; runs under search_path sali,public.

CREATE TYPE task_failure_class AS ENUM (
  'transient', 'recoverable', 'dependency', 'permission', 'fatal', 'unknown');

ALTER TABLE task_step
  ADD COLUMN attempts      int     NOT NULL DEFAULT 0,
  ADD COLUMN last_error    text,
  ADD COLUMN failure_class task_failure_class,
  ADD COLUMN verified      boolean NOT NULL DEFAULT false,
  ADD COLUMN verified_by   uuid;  -- soft ref → tool_execution.id (the verified_success backing this step)
