-- 0066: Persistent working memory — Sali's mental workspace that survives across turns,
-- context compactions, and restarts. This is NOT another memory layer — it is the active
-- cognitive state that the LLM explicitly manages: what problem is being worked on, what
-- hypotheses exist, what decisions were made, what assumptions underlie current reasoning.

CREATE TABLE IF NOT EXISTS sali.working_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id TEXT NOT NULL,
    category TEXT NOT NULL, -- 'active_problem', 'hypothesis', 'assumption', 'decision', 'planned_action', 'observation', 'question'
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'reasoning', -- 'observation', 'inference', 'tool_result', 'user_stated', 'reasoning'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ, -- NULL = persists until explicitly cleared
    superseded_by UUID, -- no FK: the new row is inserted AFTER the old row is updated
    CONSTRAINT wm_category_chk CHECK (category IN (
        'active_problem', 'hypothesis', 'assumption', 'decision',
        'planned_action', 'observation', 'question', 'context', 'directive'
    ))
);

CREATE INDEX IF NOT EXISTS ix_wm_session ON sali.working_memory (session_id, category)
    WHERE superseded_by IS NULL;

COMMENT ON TABLE sali.working_memory IS 'Sali''s persistent mental workspace — active problems, hypotheses, decisions, and assumptions that survive across turns and restarts.';
