-- Persistent organism additions: open loops, curiosities, and a proactive-decision audit trail.
--
-- These add three concepts the audit found genuinely missing from Sali's autonomy surface:
--
-- 1. open_loop        — an unresolved matter Sali is holding mental space for. Distinct from a
--                       task (formal work), a goal (an aspiration), a commitment (something
--                       promised to someone), and an initiative (a candidate ACTION scored by
--                       the InitiativeEngine). Open loops are the mid-density state: "I noticed
--                       X and haven't resolved it yet." The InitiativeEngine reads them as one
--                       of its candidate sources.
--
-- 2. curiosity        — an evidence-tracked knowledge gap. When Sali repeatedly encounters
--                       something he doesn't understand well ("this framework keeps coming up
--                       in Almir's projects"), it becomes a curiosity, with times_encountered,
--                       last_encountered_at, and a status. Idle-time background research can
--                       investigate high-priority curiosities. Deduped by subject slug.
--
-- 3. proactive_decision — every proactive-communication decision (sent OR suppressed) is
--                       logged with the reasons. This is the substrate for §46 "learn when
--                       NOT to speak": aggregate over time to identify categories that get no
--                       engagement (rate them down) and categories that consistently help
--                       (rate them up).
--
-- All three tables live in the sali schema; all are additive (no changes to existing tables).

CREATE TABLE IF NOT EXISTS sali.open_loop (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    -- Free-text description of the unresolved matter. Not shown as-is in prompts (the composer
    -- summarises); this is the durable record.
    description     TEXT NOT NULL DEFAULT '',
    -- What kind of loop this is. Enum-shape but stored as text for cheap evolution.
    kind            TEXT NOT NULL DEFAULT 'noticed'
                    CHECK (kind IN ('noticed', 'question', 'investigation', 'promise_followup',
                                    'idea', 'verification', 'monitoring')),
    -- Where the loop came from. Free-form provenance (e.g. "chat msg id X", "attention event Y",
    -- "task Z step 3 hit an anomaly").
    source          TEXT NOT NULL DEFAULT 'chat',
    source_ref      TEXT,
    -- Sali's priority (0..1). Higher = more attention. Updated when the loop is revisited.
    priority        REAL NOT NULL DEFAULT 0.4 CHECK (priority >= 0.0 AND priority <= 1.0),
    -- Optional related domain objects — nullable joins.
    goal_id         UUID REFERENCES sali.goal(id) ON DELETE SET NULL,
    task_id         UUID REFERENCES sali.task(id) ON DELETE SET NULL,
    -- Lifecycle
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'investigating', 'resolved', 'dismissed', 'expired')),
    resolution      TEXT,                        -- one-line note if resolved / dismissed
    -- When was this last touched (Sali revisited it, thought about it, updated its priority)?
    last_touched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Automatic expiry: an open loop that hasn't been touched in N days without evidence of
    -- ongoing relevance should decay to 'expired'. Nullable (null = never auto-expire).
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_open_loop_open
    ON sali.open_loop (status, priority DESC, last_touched_at DESC)
    WHERE status IN ('open', 'investigating');
CREATE INDEX IF NOT EXISTS ix_open_loop_task ON sali.open_loop (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_open_loop_expires
    ON sali.open_loop (expires_at) WHERE expires_at IS NOT NULL AND status IN ('open', 'investigating');


CREATE TABLE IF NOT EXISTS sali.curiosity (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Slug (unique per active curiosity) — used to dedup repeated encounters into one row.
    subject              TEXT NOT NULL,
    -- One-sentence, human-readable statement of the gap ("I keep seeing rust-tokio in Almir's
    -- research; I don't know it well").
    statement            TEXT NOT NULL,
    -- Why this matters (referenced project / user objective / recurring theme). Kept short.
    why_it_matters       TEXT NOT NULL DEFAULT '',
    -- Sali's own current understanding (updated when he learns more).
    current_understanding TEXT NOT NULL DEFAULT '',
    -- Priority 0..1. Higher = more likely to be picked up by idle-time investigation.
    priority             REAL NOT NULL DEFAULT 0.4 CHECK (priority >= 0.0 AND priority <= 1.0),
    -- How often has Sali encountered this subject? A rising counter is one signal for priority.
    times_encountered    INTEGER NOT NULL DEFAULT 1,
    last_encountered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Discoveries + conclusions accumulate as JSONB (a small array of {at, note}).
    discoveries          JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Lifecycle. `remaining_interesting` is the last periodic re-evaluation of whether this
    -- gap still matters (updated by the consolidation cycle); dropping it flips to 'archived'.
    status               TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open', 'investigating', 'resolved', 'archived')),
    remaining_interesting BOOLEAN NOT NULL DEFAULT true,
    -- Decay: a curiosity that hasn't been encountered in N weeks + is not marked interesting
    -- fades. Nullable (null = never decay).
    expires_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at          TIMESTAMPTZ
);

-- Dedup: one active curiosity per subject slug at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_curiosity_active
    ON sali.curiosity (subject) WHERE status IN ('open', 'investigating');
CREATE INDEX IF NOT EXISTS ix_curiosity_priority
    ON sali.curiosity (priority DESC, times_encountered DESC)
    WHERE status IN ('open', 'investigating');


CREATE TABLE IF NOT EXISTS sali.proactive_decision (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- What kind of proactive communication was CONSIDERED (may or may not have been sent).
    kind           TEXT NOT NULL,           -- e.g. 'follow_up', 'discovery', 'reminder',
                                            -- 'observation', 'social_checkin', 'greeting',
                                            -- 'opportunity', 'concern'
    -- The subject (source_ref or a short slug — commitment id, task id, environment event id).
    subject_ref    TEXT,
    -- The final decision.
    decision       TEXT NOT NULL
                   CHECK (decision IN ('sent', 'suppressed', 'deferred')),
    -- Comma-joined reason codes explaining WHY (machine-readable, e.g.
    -- "too_frequent,low_relevance"). Feeds §46 "learn when NOT to speak".
    reason_codes   TEXT NOT NULL DEFAULT '',
    -- The message text (if sent) — capped to 500 chars. Kept for the audit trail.
    message        TEXT,
    -- If a downstream signal reveals whether Almir engaged, mark it here so the aggregator can
    -- score the KIND for future decisions.
    engagement     TEXT
                   CHECK (engagement IS NULL OR engagement IN ('none', 'read', 'replied', 'acted')),
    engagement_at  TIMESTAMPTZ,
    -- When the decision was made.
    decided_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_proactive_decision_recent
    ON sali.proactive_decision (decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_proactive_decision_kind
    ON sali.proactive_decision (kind, decided_at DESC);
