-- 047: Add get_protocol_tvl to the marketplace platform tools catalog.
--
-- DeFiLlama-backed TVL lookup for DeFi protocols. Returns current TVL in USD,
-- 7-day and 30-day percentage change, per-chain breakdown, and an optional
-- daily historical series. Uses the DeFiLlama free public API (no key, no auth).
-- Priced at $0.003 (3,000 atomic USDC) — 1-2 upstream HTTP calls, free source,
-- matching resolve_ens ($0.003) in the per-call cost tier.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_protocol_tvl',
    'Protocol TVL',
    3000,
    'DeFi protocol TVL from DeFiLlama — current value, 7d/30d trend, chain breakdown, optional daily history'
)
ON CONFLICT (tool_name) DO NOTHING;
