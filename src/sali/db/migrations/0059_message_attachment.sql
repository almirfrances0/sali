-- A file Sali sent, carried ON its reply message so it renders as a downloadable card inline (one
-- durable message, not a separate bubble) and survives a reload (Almir: file showed as a separate
-- message + old files bunched on reload).
ALTER TABLE sali.message ADD COLUMN IF NOT EXISTS attachment jsonb;
