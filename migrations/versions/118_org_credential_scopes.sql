-- Migration 118: scoped, revocable org client credentials (P0.3)
-- Domain: auth / credentials
-- Invariant: additive only; existing credentials keep publish capability
-- (default 'publish') so live machine publishing behavior is unchanged.

ALTER TABLE org_client_credentials
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'publish',
    ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;

ALTER TABLE org_client_credentials
    DROP CONSTRAINT IF EXISTS chk_org_client_credentials_scope;

ALTER TABLE org_client_credentials
    ADD CONSTRAINT chk_org_client_credentials_scope
    CHECK (scope IN ('read', 'publish', 'withdraw'));

CREATE INDEX IF NOT EXISTS idx_org_client_creds_active
    ON org_client_credentials (org_id)
    WHERE disabled_at IS NULL;