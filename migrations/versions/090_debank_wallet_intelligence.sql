-- 090: Publish DeBank wallet intelligence platform tools.
-- Domain: tools / marketplace
-- Invariant: base_price_usdc is atomic USDC; seeds are idempotent.
--
-- DeBank costs 10 units ($0.002) for token_authorized_list and 15 units
-- ($0.003) for all_history_list at $0.0002/unit. Prices include platform
-- orchestration margin and remain platform-tool prices, not org marketplace
-- resale prices. DeBank resale rights require a separate Service Order.

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES
    (
        'get_wallet_approvals',
        'Wallet Approvals',
        4000,
        'DeBank token authorization exposure and protocol risk flags for one chain'
    ),
    (
        'get_wallet_history',
        'Wallet History',
        6000,
        'DeBank decoded wallet transaction history with protocol, token, exchange, and gas metadata'
    )
ON CONFLICT (tool_name) DO NOTHING;
