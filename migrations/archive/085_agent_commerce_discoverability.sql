-- 085: Agent-commerce discoverability metadata.
-- Domain: marketplace
-- Invariant: Additive, non-financial metadata only; no money-path changes.
--
-- Adds commerce-facing description and search tags to platform tools, and
-- search tags to org tools, so agent discovery surfaces can rank and match
-- tools by intent rather than exact-name substring only.

ALTER TABLE marketplace_platform_tools
    ADD COLUMN IF NOT EXISTS marketplace_description TEXT NOT NULL DEFAULT '';

ALTER TABLE marketplace_platform_tools
    ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';

ALTER TABLE org_tools
    ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_org_tools_marketplace_tags_gin
    ON org_tools USING gin (tags)
    WHERE publish_as_mcp = TRUE AND is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_platform_tools_marketplace_tags_gin
    ON marketplace_platform_tools USING gin (tags)
    WHERE is_active = TRUE;

-- Commerce-facing descriptions for flagship platform tools. These are
-- distinct from the technical `description` and are what discovery surfaces
-- (llms.txt, catalog) expose to agents evaluating a purchase.
UPDATE marketplace_platform_tools
SET marketplace_description = 'Real-time web search for current events, fact-checking, and research. Choose when the task needs external or up-to-date information.'
WHERE tool_name = 'web_search' AND marketplace_description = '';

UPDATE marketplace_platform_tools
SET marketplace_description = 'Aggregated token holdings with USD values for a wallet on Ethereum or Base. One call for portfolio exposure and risk context per chain.'
WHERE tool_name = 'get_wallet_portfolio' AND marketplace_description = '';

UPDATE marketplace_platform_tools
SET marketplace_description = 'Best-execution Uniswap v3 swap quote on Ethereum or Base. Use before executing a trade for price impact and output amount.'
WHERE tool_name = 'get_dex_quote' AND marketplace_description = '';

-- Search tags mirror the ToolDefinition tags so catalog search can match
-- intent keywords (e.g. "swap", "risk", "portfolio") beyond the name.
UPDATE marketplace_platform_tools
SET tags = ARRAY['search', 'web', 'realtime']
WHERE tool_name = 'web_search' AND tags = '{}';

UPDATE marketplace_platform_tools
SET tags = ARRAY['web3', 'ethereum', 'portfolio', 'balance', 'defi']
WHERE tool_name = 'get_wallet_portfolio' AND tags = '{}';

UPDATE marketplace_platform_tools
SET tags = ARRAY['web3', 'defi', 'uniswap', 'dex', 'quote', 'trading']
WHERE tool_name = 'get_dex_quote' AND tags = '{}';
