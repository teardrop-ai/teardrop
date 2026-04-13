-- Migration 009: A2A delegation allowlist (per-org trusted remote agents)
-- Each row authorises an org to delegate tasks to a specific A2A agent URL.
-- The UNIQUE constraint prevents duplicate entries per org.

CREATE TABLE IF NOT EXISTS a2a_allowed_agents (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    agent_url  TEXT NOT NULL,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, agent_url)
);

CREATE INDEX IF NOT EXISTS idx_a2a_allowed_agents_org
    ON a2a_allowed_agents (org_id);
