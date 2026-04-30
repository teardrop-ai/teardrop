-- 046: Seed four web3 primitives into the marketplace platform tools catalog.
--
-- These tools are fully implemented in the agent registry (tools/definitions/)
-- and have been in production use since the baseline migration, but were never
-- seeded into marketplace_platform_tools.  Without a row here, billing falls
-- through to default_cost=0, meaning all calls are effectively free.
--
-- Scope: get_eth_balance, get_erc20_balance, get_block, get_transaction.
--
-- Excluded intentionally:
--   calculate      — pure in-process math; zero marginal cost.
--   delegate_to_agent — has a dedicated a2a_delegation billing path;
--                       seeding would cause double-billing.
--   get_datetime   — in-process; zero marginal cost.
--   count_text_stats — in-process text statistics; zero marginal cost.
--
-- Pricing rationale (atomic USDC; 1_000_000 = $1.00):
--   get_eth_balance  : 1,000 ($0.001) — 1 RPC call (eth_getBalance).
--                      Below get_gas_price ($0.002); lowest utility of the set.
--   get_erc20_balance: 2,000 ($0.002) — 3 concurrent contract calls
--                      (balanceOf + symbol + decimals).  Richer output
--                      justifies parity with get_gas_price ($0.002).
--   get_block        : 1,000 ($0.001) — 1 RPC call (eth_getBlockByNumber/Hash).
--                      Same complexity as get_eth_balance.
--   get_transaction  : 2,000 ($0.002) — 2 RPC calls (eth_getTransaction +
--                      eth_getTransactionReceipt).  Two round-trips warrant
--                      the same price as get_erc20_balance.

-- get_eth_balance: native ETH balance lookup on Ethereum or Base.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_eth_balance',
    'ETH Balance',
    1000,
    'Get the native ETH balance of an Ethereum or Base address'
)
ON CONFLICT (tool_name) DO NOTHING;

-- get_erc20_balance: ERC-20 token balance with symbol and decimal normalisation.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_erc20_balance',
    'ERC-20 Balance',
    2000,
    'Get the ERC-20 token balance of a wallet, including symbol and decimals'
)
ON CONFLICT (tool_name) DO NOTHING;

-- get_block: block header and summary for Ethereum or Base.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_block',
    'Block Details',
    1000,
    'Get details for an Ethereum or Base block by number, hash, or ''latest'''
)
ON CONFLICT (tool_name) DO NOTHING;

-- get_transaction: full transaction details and receipt for Ethereum or Base.
INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_transaction',
    'Transaction',
    2000,
    'Get details and receipt for an Ethereum or Base transaction by hash'
)
ON CONFLICT (tool_name) DO NOTHING;
