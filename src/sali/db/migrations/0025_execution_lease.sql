-- 0025_execution_lease.sql — Global foreground execution lease.
-- A single-row table that tracks who owns foreground execution across all Sali processes.
-- PostgreSQL is the authoritative coordinator — not Python singletons.
--
-- Design:
-- - Single row (id='foreground') — only one foreground execution at a time globally
-- - Heartbeat-based lease — stale owners are recoverable
-- - Advisory lock for atomic acquisition — prevents race conditions
-- - Crash recovery: expired leases can be reclaimed by any process

CREATE TABLE IF NOT EXISTS execution_lease (
    id          text PRIMARY KEY DEFAULT 'foreground',
    owner_id    text NOT NULL,           -- process identifier: PID@hostname
    run_id      uuid NOT NULL,           -- the execution's canonical run_id
    session_id  uuid NOT NULL,           -- the session being executed
    origin      text NOT NULL DEFAULT 'cli',  -- cli | api | ios | scheduler
    acquired_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL DEFAULT now() + interval '5 minutes',
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'releasing'))
);

-- Seed the single row (idempotent)
INSERT INTO execution_lease (id, owner_id, run_id, session_id, status)
VALUES ('foreground', 'none', gen_random_uuid(), gen_random_uuid(), 'releasing')
ON CONFLICT (id) DO NOTHING;
