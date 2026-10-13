-- 0040_natural_consent.sql — Human interaction, judgment & natural consent (Prompt 11).
-- Additive + minimal (§66/§73/§78: reuse, no giant confirmation framework, no duplicate systems). The
-- attention classifier (conversation vs work), waiting_for_user clarification, the authority ladder
-- (runtime/authority.py, Prompt 10), commitments, external identities, the decision ledger, memory +
-- provenance + contradiction, person/relationship, behavior evolution (preferences + revocation), the
-- reviewer, and proactive messaging all already exist (0030-0039). This adds only the missing primitive:
-- a durable, SCOPED, EXPIRING natural-consent request — structured state underneath, natural language on
-- top (§57). It is NOT a y/n gate framework: the user's free-text reply is the consent signal.
CREATE TABLE consent_request (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         uuid REFERENCES task(id) ON DELETE SET NULL,
  run_id          uuid,
  action          text NOT NULL,             -- what Sali proposes to do, in plain language
  capability      text,
  scope           text,                      -- the target/scope the consent would authorize (§5)
  consequence     text,                      -- plain-language consequence (reversibility, externality)
  rationale       text,
  judgment_level  text NOT NULL DEFAULT 'consent'
                  CHECK (judgment_level IN ('low','normal','attention','consent','high_consequence','blocked')),
  reversible      boolean,
  external        boolean,
  standing        boolean NOT NULL DEFAULT false,   -- a durable standing authorization, not a one-off (§38)
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','granted','modified','declined','deferred','expired','revoked')),
  response        text,                       -- the user's natural-language reply (the consent signal)
  granted_scope   text,                       -- the (possibly narrowed) scope actually authorized (§5)
  parent_consent  uuid REFERENCES consent_request(id) ON DELETE SET NULL,   -- inheritance (§59)
  expires_at      timestamptz,                -- consent can naturally expire (§58)
  created_at      timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz);
CREATE INDEX ix_consent_pending ON consent_request (status) WHERE status = 'pending';
CREATE INDEX ix_consent_task ON consent_request (task_id);
-- a live standing authorization is looked up by scope; only one active standing grant per scope
CREATE UNIQUE INDEX ux_consent_standing ON consent_request (scope)
  WHERE standing AND status IN ('granted','modified');
