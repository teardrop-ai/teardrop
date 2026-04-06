-- Migration 005: org-scoped M2M client credentials
-- Allows per-org machine-to-machine credentials stored in the database,
-- separate from the environment-variable-based fallback credential.
--
-- Secrets are stored as PBKDF2-SHA256 hashes (same scheme as users.salt/hashed_secret).
-- The plaintext client_secret is returned to the caller exactly once at creation time.

CREATE TABLE IF NOT EXISTS org_client_credentials (
    client_id     TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    hashed_secret TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_client_creds_org ON org_client_credentials (org_id);
