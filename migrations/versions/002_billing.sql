-- Migration 002: billing schema
-- Extends usage_events with settlement columns and adds pricing_rules table.

-- ── Extend usage_events ───────────────────────────────────────────────────────
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_usdc BIGINT NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS settlement_tx TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS settlement_status TEXT NOT NULL DEFAULT 'none';

CREATE INDEX IF NOT EXISTS idx_usage_settlement
    ON usage_events (settlement_status, created_at)
    WHERE settlement_status != 'none';

-- ── Pricing rules ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pricing_rules (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    run_price_usdc        BIGINT NOT NULL DEFAULT 10000,   -- $0.01 in 6-decimal atomic units
    tokens_in_cost_per_1k BIGINT NOT NULL DEFAULT 0,       -- reserved for upto scheme
    tokens_out_cost_per_1k BIGINT NOT NULL DEFAULT 0,      -- reserved for upto scheme
    tool_call_cost        BIGINT NOT NULL DEFAULT 0,       -- reserved for upto scheme
    effective_from        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed default pricing rule
INSERT INTO pricing_rules (id, name, run_price_usdc)
VALUES ('default', 'Default pricing', 10000)
ON CONFLICT (id) DO NOTHING;
