-- 033: Add get_token_approvals to the marketplace platform tools catalog.
--
-- ERC-20 allowance audit tool — returns all non-zero approvals for a wallet
-- across curated DeFi protocol spenders with unlimited-approval risk flags.
-- Priced at $0.004 (4,000 atomic USDC) per call, matching get_wallet_portfolio.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_token_approvals',
    'Token Approvals',
    4000,
    'ERC-20 allowance audit across curated DeFi spenders with unlimited-approval risk flags'
)
ON CONFLICT (tool_name) DO NOTHING;
