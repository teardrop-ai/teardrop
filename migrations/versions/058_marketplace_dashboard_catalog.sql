-- 058: Marketplace dashboard catalog support.
-- Domain: marketplace
-- Invariant: Supports dashboard catalog views; no monetary change
--
-- Adds public-safe catalog metadata for O4 dashboard/SEO pages:
--   - category filters for community and platform marketplace tools
--   - aggregate call stats decoupled from financial author earnings
--
-- Financial ledgers remain unchanged.  All money fields continue to use
-- atomic USDC integers; this migration only adds non-financial metadata.

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT ''
    CHECK (category IN ('', 'defi', 'search', 'data', 'communication', 'utility'));

ALTER TABLE marketplace_platform_tools
    ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT ''
    CHECK (category IN ('', 'defi', 'search', 'data', 'communication', 'utility'));

CREATE TABLE IF NOT EXISTS marketplace_tool_call_stats (
    qualified_tool_name TEXT PRIMARY KEY,
    tool_type           TEXT NOT NULL CHECK (tool_type IN ('platform', 'community')),
    author_org_id       TEXT REFERENCES orgs(id),
    total_calls         BIGINT NOT NULL DEFAULT 0 CHECK (total_calls >= 0),
    first_call_at       TIMESTAMPTZ,
    last_call_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_marketplace_tool_call_stats_popularity
    ON marketplace_tool_call_stats (total_calls DESC, qualified_tool_name ASC);

CREATE INDEX IF NOT EXISTS idx_marketplace_tool_call_stats_author
    ON marketplace_tool_call_stats (author_org_id, total_calls DESC)
    WHERE author_org_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_org_tools_catalog_category
    ON org_tools (category, publish_as_mcp, is_active, name)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_marketplace_platform_tools_category
    ON marketplace_platform_tools (category, tool_name)
    WHERE is_active = TRUE;

-- Best-effort category seeds for platform tools. Community tool category is
-- author-controlled through the org tool API and defaults to uncategorized.
UPDATE marketplace_platform_tools
SET category = 'search'
WHERE tool_name IN ('web_search', 'http_fetch') AND category = '';

UPDATE marketplace_platform_tools
SET category = 'defi'
WHERE tool_name IN (
    'get_wallet_portfolio',
    'get_token_price',
    'get_token_price_historical',
    'get_protocol_tvl',
    'get_yield_rates',
    'get_lending_rates',
    'get_dex_quote',
    'get_defi_positions',
    'get_liquidation_risk',
    'get_token_approvals'
) AND category = '';

UPDATE marketplace_platform_tools
SET category = 'data'
WHERE tool_name IN (
    'get_eth_balance',
    'get_erc20_balance',
    'get_block',
    'get_transaction',
    'convert_currency'
) AND category = '';

COMMENT ON TABLE marketplace_tool_call_stats IS
    'Public-safe aggregate marketplace call counts for dashboard/SEO catalog pages. Not a financial ledger.';