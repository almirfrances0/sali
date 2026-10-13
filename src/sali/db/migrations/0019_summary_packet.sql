-- 0019_summary_packet.sql — machine-readable continuation packet beside the prose summary (§11-12).
-- The running `summary` (0006) stays as the human-readable carry-forward; this adds a structured jsonb
-- sibling holding the SAME handoff as discrete fields — the active task + step ticks + next step +
-- failed steps (deterministic, from task_step), plus the model's labeled notes (done/decisions/facts/
-- open…). So the continuation is queryable and the critical task state can never be lost to prose.
-- Additive, forward-only. NOT a parallel table — one row per conversation, same as `summary`.

ALTER TABLE conversation ADD COLUMN summary_packet jsonb;
