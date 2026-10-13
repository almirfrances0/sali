-- 0065: Protected memory rules — safety-critical facts that must never be retired by forget().
-- Protected memories cannot be forgotten, superseded by lower-priority sources, or expired.
-- They represent inviolable rules (e.g., "never delete /home/almir/Desktop/sali").

ALTER TABLE sali.memory ADD COLUMN IF NOT EXISTS protected boolean NOT NULL DEFAULT false;

-- Index for the forget() guard — it checks protected before closing the validity interval.
CREATE INDEX IF NOT EXISTS ix_memory_protected ON sali.memory (id) WHERE protected AND valid_until IS NULL;

COMMENT ON COLUMN sali.memory.protected IS 'Safety-critical rule: cannot be retired by forget(), superseded by lower-priority sources, or expired by retention GC.';
