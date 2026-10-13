-- Password auth: the app authenticates with a PASSWORD (not a device pairing code), and the password is
-- the DURABLE credential the app re-authenticates with — so an expired or revoked token can never lock the
-- owner out (the whole point). The password is stored ONE-WAY (salted scrypt, api/password.py), never in
-- plaintext and never reversibly. It lives on the sali_state singleton (id=true). NULL = not set yet; the
-- daemon seeds the default at boot, NULL-guarded so a restart never clobbers a rotated password.
ALTER TABLE sali.sali_state
    ADD COLUMN IF NOT EXISTS auth_password_hash TEXT,
    ADD COLUMN IF NOT EXISTS auth_password_set_at timestamptz;

COMMENT ON COLUMN sali.sali_state.auth_password_hash IS
    'One-way salted scrypt hash of the API login password (api/password.py). NULL = unset; seeded at boot.';
