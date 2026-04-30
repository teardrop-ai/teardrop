-- 048: Add get_yield_rates to the marketplace platform tools catalog.
--
-- DeFiLlama /pools aggregator — returns yield pools filtered by protocol,
-- chain, minimum TVL, and minimum APY, sorted by APY descending. Covers
-- Aave, Compound, Curve, Yearn, and 1,000+ other protocols across all chains.
-- Single upstream HTTP call; filtering is client-side from the full pool list.
-- Priced at $0.004 (4,000 atomic USDC) — larger response payload and
-- filtering compute, matching get_token_price_historical in the cost tier.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_yield_rates',
    'Yield Rates',
    4000,
    'DeFi yield pool rates from DeFiLlama — APY, TVL, and token breakdown across 1,000+ protocols and chains'
)
ON CONFLICT (tool_name) DO NOTHING;
