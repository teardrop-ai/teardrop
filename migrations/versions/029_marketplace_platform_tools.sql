-- 029: Platform tools in the marketplace catalog.
--
-- Platform-built tools (tools/definitions/) are distinct from org-published
-- tools (org_tools).  They execute in-process, have no webhook URL, and are
-- owned by the platform rather than a specific org.
--
-- This table + seed lets get_marketplace_catalog() UNION platform tools with
-- org tools, giving agents a single catalog view.
--
-- base_price_usdc uses atomic USDC (6 decimals): 1_000_000 = $1.00.

CREATE TABLE IF NOT EXISTS marketplace_platform_tools (
    tool_name       TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    base_price_usdc BIGINT NOT NULL DEFAULT 0
        CHECK (base_price_usdc >= 0 AND base_price_usdc <= 100000000),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mpt_active
    ON marketplace_platform_tools(tool_name)
    WHERE is_active = TRUE;

COMMENT ON TABLE marketplace_platform_tools IS
    'Platform-owned tools published to the marketplace catalog. No org_id owner.';

-- Seed the initial 5 monetised platform tools.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    ('get_wallet_portfolio', 'Wallet Portfolio',  4000,  'Multi-chain wallet balances with live token prices in one call'),
    ('web_search',           'Web Search',        10000, 'Real-time web search powered by Tavily'),
    ('get_token_price',      'Token Price',       2000,  'Live crypto prices via CoinGecko with 60s cache, batch up to 50 tokens'),
    ('http_fetch',           'HTTP Fetch',        2000,  'SSRF-protected URL fetch with clean text extraction'),
    ('convert_currency',     'Currency Convert',  2000,  'Fiat and crypto currency conversion in one call')
ON CONFLICT (tool_name) DO NOTHING;
