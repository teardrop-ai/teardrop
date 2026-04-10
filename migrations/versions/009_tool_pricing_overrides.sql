-- Migration 009: per-tool pricing overrides
-- Adds tool_pricing_overrides table that lets admins set a custom cost_usdc
-- for individual tools, overriding the flat tool_call_cost from pricing_rules.
--
-- cost_usdc uses the same atomic USDC convention (6-decimal integer):
--   1_000_000 = $1.00,  15_000 = $0.015,  2_000 = $0.002,  1_000 = $0.001

CREATE TABLE IF NOT EXISTS tool_pricing_overrides (
    tool_name   TEXT        PRIMARY KEY,
    cost_usdc   BIGINT      NOT NULL CHECK (cost_usdc >= 0),
    description TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed initial overrides for tools whose COGS exceed the flat tool_call_cost.
--   web_search:         $0.015  (Tavily costs $0.008/call; 15000 gives ~47% margin)
--   get_token_price:    $0.002  (CoinGecko overage $0.0005/call; 2× premium for market data)
--   get_wallet_portfolio: $0.005 (value pricing — replaces 5–15 individual calls)

INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
VALUES
    ('web_search',           15000, 'Tavily search API — covers COGS with margin'),
    ('get_token_price',       2000, 'CoinGecko market data — premium for external API dependency'),
    ('get_wallet_portfolio',  5000, 'Value pricing — aggregates multiple on-chain queries')
ON CONFLICT (tool_name) DO NOTHING;
