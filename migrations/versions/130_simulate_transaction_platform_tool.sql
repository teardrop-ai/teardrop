-- Migration 130: register simulate_transaction platform tool
-- Domain: marketplace / tools / web3
-- Invariant: atomic USDC values are BIGINT (10000 atomic = $0.01 USDC); idempotent insert.
-- Without this row, marketplace-enabled pricing treats the bare tool name as a free org tool.

INSERT INTO marketplace_platform_tools (
    tool_name,
    display_name,
    base_price_usdc,
    description,
    category,
    tags,
    marketplace_description
)
VALUES (
    'simulate_transaction',
    'Simulate Transaction',
    10000,
    'Dry-run a transaction or ordered bundle on Ethereum or Base: per-call success or revert reason, gas, net balance and approval changes, and a receipt hash',
    'defi',
    ARRAY['web3', 'simulation', 'transaction', 'defi', 'risk'],
    'Lets an agent confirm a transaction will succeed and see exactly what it moves before acting on-chain'
)
ON CONFLICT (tool_name) DO NOTHING;
