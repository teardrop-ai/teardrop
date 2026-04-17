-- 026: Add jwt_forward flag to a2a_allowed_agents
-- Allows per-agent opt-in to forward caller JWT as Authorization header.

ALTER TABLE a2a_allowed_agents
    ADD COLUMN IF NOT EXISTS jwt_forward BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN a2a_allowed_agents.jwt_forward IS
    'When TRUE, forward caller JWT as Authorization header to this agent.';
