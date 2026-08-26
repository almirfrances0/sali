-- 0006_compaction.sql — the one continuous session never breaks.
-- When a conversation grows long, older turns fold into a running summary Sali carries
-- forward, so context stays small while memory stays continuous.
ALTER TABLE conversation ADD COLUMN summary text;
ALTER TABLE conversation ADD COLUMN summary_through_seq int NOT NULL DEFAULT 0;
