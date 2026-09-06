-- Sali's SELF-CLAIM contradiction ledger (§32/§44/§45). When the response-claim validator
-- (verify/response_claims) catches Sali about to assert something false about his OWN action,
-- state, capability, or a file he didn't send, it strikes the sentence and rewrites it — and
-- records the strike HERE, durably. This is the difference between a correction that vanishes
-- with the turn and one Sali can be measured by: how often he over-claims, in which family, and
-- whether it is getting better. A ROW PER STRUCK CLAIM (not per turn) so a single reply that
-- over-claimed twice counts twice, and the metrics sink (GET /grounding) sums honestly.
CREATE TABLE IF NOT EXISTS sali.grounding_event (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    at          timestamptz NOT NULL DEFAULT now(),
    session_id  uuid,
    run_id      uuid,
    kind        text NOT NULL,                    -- claim family: action_done | state | capability | file_send
    verdict     text NOT NULL DEFAULT 'unsupported',
    sentence    text NOT NULL,                    -- the sentence that was struck (Sali's own words)
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb -- validator's reason/evidence label for the strike
);
CREATE INDEX IF NOT EXISTS ix_grounding_event_at ON sali.grounding_event (at DESC);
CREATE INDEX IF NOT EXISTS ix_grounding_event_kind ON sali.grounding_event (kind);
