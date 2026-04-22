-- 031: Activate four bench tools in the marketplace catalog.
--
-- get_gas_price, resolve_ens, read_contract, and decode_transaction were
-- previously implemented (tools/definitions/) but not exposed as billed
-- marketplace tools.  This migration seeds their catalog rows so that
-- get_marketplace_catalog() includes them and the MCP gateway charges callers.
--
-- Pricing rationale (atomic USDC, 6 decimals; 1_000_000 = $1.00):
--   get_gas_price    2 000  ($0.002) — high-volume, cached 10 s; same tier as token price
--   resolve_ens      3 000  ($0.003) — forward + reverse lookup + avatar; slightly richer than price
--   read_contract    5 000  ($0.005) — general-purpose power tool; arbitrary contract reads
--   decode_transaction 5 000 ($0.005) — tx + receipt fetch + 4byte lookup; high-information output

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    (
        'get_gas_price',
        'Gas Price',
        2000,
        'Current EIP-1559 gas fees on Ethereum or Base: base fee, priority fee, '
        'next-block base fee estimate, and network congestion ratio.'
    ),
    (
        'resolve_ens',
        'ENS Resolve',
        3000,
        'Forward lookup (ENS name → address) or reverse lookup (address → primary ENS name). '
        'Returns avatar text record when available.'
    ),
    (
        'read_contract',
        'Read Contract',
        5000,
        'Call any view/pure function on a smart contract with your ABI fragment. '
        'Supports historical queries via block number. State-changing calls are rejected.'
    ),
    (
        'decode_transaction',
        'Decode Transaction',
        5000,
        'Decode transaction calldata into function name and arguments. '
        'Returns status (success/revert), gas used, and block number. '
        'Uses provided ABI or falls back to 4byte.directory.'
    )
ON CONFLICT (tool_name) DO NOTHING;
