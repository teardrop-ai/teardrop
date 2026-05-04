-- 052: Add get_lending_rates to the marketplace platform tools catalog.
--
-- On-chain lending-rate snapshot for Aave v3 and Compound v3 across Ethereum
-- and Base. The tool fans out via Multicall3 and returns per-asset supply and
-- borrow APY plus Compound utilization.
--
-- Pricing rationale:
--   get_lending_rates => $0.003 (3,000 atomic USDC)
--   - More RPC-intensive than get_protocol_tvl/get_yield_rates HTTP lookups,
--     but still deterministic read-only calls with bounded payload.
--   - Keeps parity with get_protocol_tvl ($0.003) while remaining below the
--     broad market scan tier represented by get_yield_rates ($0.004).

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_lending_rates',
    'Lending Rates',
    3000,
    'On-chain lending APY snapshot for Aave v3 and Compound v3 by asset and chain'
)
ON CONFLICT (tool_name) DO NOTHING;
