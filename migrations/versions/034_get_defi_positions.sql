-- 034: Add get_defi_positions to the marketplace platform tools catalog.
--
-- DeFi position aggregator across Aave v3, Compound v3, and Uniswap v3 LP on
-- Ethereum mainnet and Base. Aggregates account-level health (collateral,
-- debt, liquidation risk), per-reserve breakdowns, and LP position data.
-- Priced at $0.013 (13,000 atomic USDC) per call — highest platform tool
-- reflecting multi-protocol RPC cost and differentiated DeFi value.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_defi_positions',
    'DeFi Positions',
    13000,
    'Aggregate DeFi positions across Aave v3, Compound v3, and Uniswap v3 LP on Ethereum and Base'
)
ON CONFLICT (tool_name) DO NOTHING;
