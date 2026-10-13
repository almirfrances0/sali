-- Drop the orphaned `delegation` table.
--
-- The DelegationStore + Delegate/_DelegateSink refuse-scaffold was removed in the brain-audit
-- (single-executive-agent invariant is now enforced by ABSENCE, not by a refusing class). That left
-- the `delegation` table (created in 0033_cognitive_os.sql) with no writer, no reader, and 0 rows —
-- a pure orphan. Verified before this migration: nothing in src/ issues FROM/INTO/UPDATE against it
-- (the many "delegation" mentions in code are the unrelated "run it" command-approval flow —
-- is_delegation()/pending.py — which touches `pending_action`, not this table). Forward-only cleanup.
DROP TABLE IF EXISTS sali.delegation CASCADE;
