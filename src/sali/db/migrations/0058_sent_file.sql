-- Files Sali SENDS to Almir on his device (Almir: "i can tell sali to send any file, zip and send any
-- file to me so i can download"). He is on his iPhone, not at the PC, so a file written to a local path
-- never reaches him — send_file registers a downloadable copy here and the app receives it as a file.
CREATE TABLE IF NOT EXISTS sali.sent_file (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid,
    run_id      uuid,
    filename    text NOT NULL,
    path        text NOT NULL,          -- absolute path to the served copy under sali-works/sends/
    bytes       integer NOT NULL DEFAULT 0,
    kind        text,                   -- 'file' | 'zip'
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sent_file_run ON sali.sent_file (run_id);
CREATE INDEX IF NOT EXISTS ix_sent_file_created ON sali.sent_file (created_at DESC);
