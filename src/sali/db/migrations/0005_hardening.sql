-- 0005_hardening.sql — review fixes.
-- Twin substrate (sali2 §4): a node's identity has both a "last observed" and a "last
-- verified" time. `last_verified` was doing double duty; split out `last_seen`, bumped on
-- every re-observation, and reserve `last_verified` for explicit verification.
ALTER TABLE graph_node ADD COLUMN last_seen timestamptz NOT NULL DEFAULT now();
