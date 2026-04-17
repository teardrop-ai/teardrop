-- 023_siwe_login_sessions.sql
-- SIWE QR login sessions for the CLI → browser → wallet sign flow.
--
-- A session ties a single-use nonce to a session_id so that:
--   1. The CLI can create the session (POST /auth/siwe/sessions) and poll for completion.
--   2. The browser signing page can read the nonce/domain (GET /auth/siwe/sessions/{id})
--      and submit the signed message (POST /auth/siwe/sessions/{id}/complete).
-- The access_token and refresh_token columns are populated only when status='complete'.

CREATE TABLE IF NOT EXISTS siwe_login_sessions (
    session_id    TEXT        PRIMARY KEY,
    nonce         TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',  -- 'pending' | 'complete'
    access_token  TEXT,
    refresh_token TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL
);

-- Used by a periodic cleanup job and for expiry filtering on reads.
CREATE INDEX IF NOT EXISTS idx_siwe_login_sessions_expires
    ON siwe_login_sessions (expires_at);
