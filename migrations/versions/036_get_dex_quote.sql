-- 036: Add get_dex_quote to the marketplace platform tools catalog.
--
-- On-chain Uniswap v3 swap quote via direct QuoterV2 calls on Ethereum
-- mainnet and Base. Queries all four fee tiers (100/500/3000/10000 bps)
-- in parallel and returns the best amountOut. Pure RPC — no external
-- aggregator dependency. Flat-priced at $0.005 (5,000 atomic USDC) per
-- call, reflecting ~4–6 eth_calls per invocation and free-tier
-- competition from Uniswap frontend / 1inch public API.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_dex_quote',
    'DEX Quote',
    5000,
    'Best Uniswap v3 swap quote across all fee tiers on Ethereum and Base, via direct on-chain QuoterV2 calls'
)
ON CONFLICT (tool_name) DO NOTHING;
