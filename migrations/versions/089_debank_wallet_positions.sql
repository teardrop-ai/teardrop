-- 089: Publish the DeBank-backed wallet positions tool.
-- Domain: tools / marketplace
-- Invariant: base_price_usdc is atomic USDC; the seed is idempotent.
--
-- The $0.020 platform price covers the default DeBank request pair
-- (all_complex_protocol_list = 30 units + total_balance = 30 units = 60 units
-- = $0.012 at $0.0002/unit) with ~40% margin for platform orchestration.
-- Positions-only mode (include_net_worth=false) costs 30 units = $0.006.
-- The tool remains usable without marketplace mode; this row makes its
-- external provider cost discoverable and separately attributable.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_wallet_positions',
    'Wallet Positions',
    20000,
    'All-chain DeBank protocol positions, token lists, and optional net worth for an EVM wallet'
)
ON CONFLICT (tool_name) DO NOTHING;