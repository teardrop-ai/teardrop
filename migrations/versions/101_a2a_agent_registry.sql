-- Migration 101: A2A agent registry (per-org remote agent endpoints)
-- Domain: A2A / delegation
-- Invariant: agent_url must be https-only (SSRF-safe); registry is per-org; failure_origin is additive telemetry
--
-- Registers each org's remote A2A agent endpoint so inbound delegation can be
-- routed to the correct agent URL. Also adds failure_origin to
-- a2a_delegation_events so operators can distinguish local vs remote failures.

CREATE TABLE IF NOT EXISTS a2a_agent_registry (
    org_id      TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    agent_url   TEXT NOT NULL UNIQUE CHECK (agent_url ~ '^https://[^[:space:]]+$'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE a2a_delegation_events
    ADD COLUMN IF NOT EXISTS failure_origin TEXT NOT NULL DEFAULT 'unknown'
        CHECK (failure_origin IN ('unknown', 'local', 'remote'));

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_events_agent_url_created
    ON a2a_delegation_events (rtrim(agent_url, '/'), created_at DESC);