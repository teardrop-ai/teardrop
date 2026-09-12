-- Migration 107: immutable MCP billing outcome events for machine-volume measurement
-- Domain: MCP / billing telemetry
-- Invariant: only calls that reached a settlement decision are recorded; payment payloads and tool arguments are excluded.

CREATE TABLE IF NOT EXISTS mcp_call_events (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL DEFAULT '',
    payer_address     TEXT NOT NULL DEFAULT '',
    tool_name         TEXT NOT NULL,
    billing_method    TEXT NOT NULL CHECK (billing_method IN ('x402', 'credit')),
    cost_usdc         BIGINT NOT NULL CHECK (cost_usdc >= 0),
    settlement_status TEXT NOT NULL CHECK (settlement_status IN ('settled', 'failed')),
    settlement_tx     TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mcp_call_events_created_at
    ON mcp_call_events (created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_call_events_payer
    ON mcp_call_events (payer_address)
    WHERE payer_address <> '';
CREATE INDEX IF NOT EXISTS idx_mcp_call_events_method_created
    ON mcp_call_events (billing_method, created_at DESC);

COMMENT ON TABLE mcp_call_events IS 'Immutable MCP billing outcomes; excludes payment payloads and tool arguments.';