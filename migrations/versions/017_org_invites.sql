-- Migration 017: org member invites
-- Token-authenticated invitation flow for adding users to an existing org
-- without requiring a Teardrop platform admin.

CREATE TABLE IF NOT EXISTS org_invites (
    token      TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email      TEXT,
    role       TEXT NOT NULL DEFAULT 'user',
    invited_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_org_invites_org ON org_invites (org_id);
