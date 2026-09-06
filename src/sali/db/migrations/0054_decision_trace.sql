-- Decision-trace ledger for autonomous cognitive decisions (§29 metacognition, §44 traceability).
--
-- Every meaningful autonomous decision the cognitive loop makes writes ONE compact structured
-- row here. This is NOT chain-of-thought (deliberately not stored) — it's the operational
-- reasoning metadata Almir can query later ("why did Sali decide to act on X at 4pm?").
--
-- Rows are cheap: text keys + jsonb evidence. Retention is bounded by a periodic pruner
-- (dropped after 30 days unless explicitly pinned, e.g. tied to a still-open initiative). The
-- rollup query for the /cognitive-metrics endpoint reads counts by (mode, outcome) buckets.

CREATE TABLE IF NOT EXISTS sali.decision_trace (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The decision the cognitive loop chose from the discrete set §10 defines.
    mode            TEXT NOT NULL
                    CHECK (mode IN ('act', 'communicate', 'learn', 'observe', 'wait',
                                    'defer', 'ask', 'abort')),
    -- What the decision was ABOUT — a subject slug the caller supplies (initiative id,
    -- open_loop id, curiosity subject, task id, or free-form "chat:hello").
    subject_ref     TEXT,
    -- Where the driver came from — 'initiative_engine', 'coordinator', 'proactive_loop', etc.
    origin          TEXT NOT NULL DEFAULT 'initiative_engine',
    -- Compact machine-readable reason codes (comma-joined). Feeds aggregate analysis.
    reason_codes    TEXT NOT NULL DEFAULT '',
    -- Compact confidence 0..1 the driver had in this decision at the moment it fired.
    confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    -- Structured evidence: the small dict the driver used (initiative source, priority score,
    -- resource snapshot, etc.). Not free-form prose — always keyed data.
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Predicted outcome (short slug, e.g. 'file_created', 'user_reply', 'no_action').
    expected_outcome TEXT,
    -- Actual outcome once observed. Nullable — set by a follow-up call once the world responds.
    actual_outcome  TEXT,
    outcome_at      TIMESTAMPTZ,
    -- Whether policy accepted this decision. Feeds §40 "policy has final say" traceability.
    policy_result   TEXT DEFAULT 'accepted'
                    CHECK (policy_result IN ('accepted', 'refused', 'deferred')),
    -- Whether a learning event fired from this decision (yes/no + optional lesson id).
    learning_id     UUID,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_decision_trace_recent
    ON sali.decision_trace (decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_decision_trace_mode
    ON sali.decision_trace (mode, decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_decision_trace_subject
    ON sali.decision_trace (subject_ref, decided_at DESC) WHERE subject_ref IS NOT NULL;
