-- 0044_ios_control_center.sql — Secure remote presence for the Sali iPhone control center (Prompt 13).
-- Additive only (§44: never break stable systems). The existing REST/WS surface stays; this adds the
-- durable substrate for device enrollment + device-bound short-lived auth + device authorization, so the
-- API can be exposed through sali.salieno.com behind a Cloudflare Tunnel without becoming a bypass around
-- Sali's existing safety, workspace, and task authority.
--
-- SECRETS DISCIPLINE (§21/§22): no plaintext secret is ever stored. Enrollment codes, access tokens, and
-- refresh tokens are persisted only as SHA-256 hashes; the plaintext exists just once, in the response that
-- issues it. Push tokens are opaque APNs identifiers, not credentials.

-- ── An enrolled client device (an iPhone) — trust is server-side, not baked into the app binary (§23) ──
CREATE TABLE api_device (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name             text NOT NULL,                       -- human label ("Almir's iPhone 15 Pro")
  platform         text NOT NULL DEFAULT 'ios',
  model            text,                                -- reported device model (e.g. iPhone16,1)
  role             text NOT NULL DEFAULT 'owner'        -- authorization tier (§27)
                   CHECK (role IN ('owner', 'controller', 'observer')),
  status           text NOT NULL DEFAULT 'active'       -- revoke a lost/compromised device (§24)
                   CHECK (status IN ('active', 'revoked')),
  push_token       text,                                -- APNs device token (opaque; §11), not a secret
  push_environment text CHECK (push_environment IN ('sandbox', 'production')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at     timestamptz,
  revoked_at       timestamptz);
CREATE INDEX ix_api_device_status ON api_device (status, created_at DESC);

-- ── A one-time, short-lived pairing secret minted ON THE HOST (§23). Stored hashed. Single use. ───────
-- The host (which holds the local API token) mints a code; the fresh iPhone exchanges it for a device
-- session. The code MUST expire and MUST NOT become a permanent credential.
CREATE TABLE enrollment_code (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code_hash   text NOT NULL,                            -- sha256(normalized code); plaintext never persists
  role        text NOT NULL DEFAULT 'owner'
              CHECK (role IN ('owner', 'controller', 'observer')),
  label       text,                                     -- suggested device label
  created_by  text NOT NULL DEFAULT 'host',
  expires_at  timestamptz NOT NULL,                     -- hard expiry (§23) — enforced on redemption
  used_at     timestamptz,                              -- single-use: set on redemption, never reusable
  device_id   uuid REFERENCES api_device (id) ON DELETE SET NULL,  -- the device it enrolled, once used
  created_at  timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_enrollment_code_hash ON enrollment_code (code_hash);
CREATE INDEX ix_enrollment_code_open ON enrollment_code (expires_at) WHERE used_at IS NULL;

-- ── A device's live session: a SHORT-lived access token + a longer, rotating refresh token (§24). ─────
-- Only hashes are stored. Revoking a device (or a session) invalidates every request bound to it.
CREATE TABLE device_session (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id          uuid NOT NULL REFERENCES api_device (id) ON DELETE CASCADE,
  access_hash        text NOT NULL,                     -- sha256(access token) — short TTL
  refresh_hash       text NOT NULL,                     -- sha256(refresh token) — longer TTL, rotated on use
  access_expires_at  timestamptz NOT NULL,
  refresh_expires_at timestamptz NOT NULL,
  revoked            boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now(),
  rotated_at         timestamptz);
CREATE UNIQUE INDEX ux_device_session_access ON device_session (access_hash);
CREATE UNIQUE INDEX ux_device_session_refresh ON device_session (refresh_hash);
CREATE INDEX ix_device_session_device ON device_session (device_id) WHERE NOT revoked;
