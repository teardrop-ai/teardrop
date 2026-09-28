-- Migration 113: self-serve A2A allowlisting of marketplace-registered agents.
-- Domain: A2A / billing
-- Invariant: self_serve rows always require x402, never forward the caller JWT, and carry a positive
-- per-row cap; registry membership is re-checked at delegation time. Allowlist changes are append-only events.

ALTER TABLE a2a_allowed_agents
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'admin';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'a2a_allowed_agents_source_check'
    ) THEN
        ALTER TABLE a2a_allowed_agents
            ADD CONSTRAINT a2a_allowed_agents_source_check CHECK (source IN ('admin', 'self_serve'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'a2a_allowed_agents_self_serve_guard'
    ) THEN
        ALTER TABLE a2a_allowed_agents
            ADD CONSTRAINT a2a_allowed_agents_self_serve_guard
            CHECK (source <> 'self_serve' OR (require_x402 AND NOT jwt_forward AND max_cost_usdc > 0));
    END IF;
END $$;

COMMENT ON COLUMN a2a_allowed_agents.source IS
    'admin = created by an org admin (any URL); self_serve = member-created for a marketplace-registered agent.';

CREATE TABLE IF NOT EXISTS a2a_allowed_agent_events (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL,
    allowed_agent_id TEXT NOT NULL,
    agent_url        TEXT NOT NULL,
    event_type       TEXT NOT NULL CHECK (event_type IN ('created', 'deleted')),
    source           TEXT NOT NULL,
    max_cost_usdc    BIGINT NOT NULL DEFAULT 0,
    actor_id         TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_a2a_allowed_agent_events_org_created
    ON a2a_allowed_agent_events (org_id, created_at DESC);
