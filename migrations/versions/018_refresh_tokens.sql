-- Migration 018: refresh tokens
-- Long-lived tokens (default 30 days) exchanged for short-lived access tokens
-- (30 min). Tokens are rotated on every use per OWASP best practice.
-- Covers email and SIWE auth flows.

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token        TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id       TEXT NOT NULL,
    auth_method  TEXT NOT NULL,
    extra_claims JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id);
