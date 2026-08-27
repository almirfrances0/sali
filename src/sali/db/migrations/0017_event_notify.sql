-- 0017_event_notify.sql — turn the durable event log into a push bus (spec §14).
-- Producers already write the append-only `event` table; consumers (proactive, investigate, ...) each
-- hand-rolled a watermark poll on a fixed interval, so a new event waited up to that interval to be
-- noticed. This AFTER INSERT trigger fires pg_notify on the 'sali_events' channel with the new seq, so
-- a LISTENing consumer is woken the instant an event commits and polls from its watermark immediately —
-- push instead of poll, with ZERO change to any writer and the table remaining the durable spine.

CREATE OR REPLACE FUNCTION sali_notify_event() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('sali_events', NEW.seq::text);
  RETURN NULL;  -- AFTER trigger: return value is ignored
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_event_notify AFTER INSERT ON event
  FOR EACH ROW EXECUTE FUNCTION sali_notify_event();
