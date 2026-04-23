-- 035: Add get_liquidation_risk to the marketplace platform tools catalog.
--
-- Per-wallet DeFi liquidation risk assessment across Aave v3 and Compound v3
-- on Ethereum mainnet and Base. Accepts up to 50 wallet addresses per call
-- and returns tiered risk classification (liquidatable/critical/warning/
-- caution/healthy/no_debt) with an aggregate overall_tier across protocols.
-- Flat-priced at $0.010 (10,000 atomic USDC) per call — priced assuming
-- batched usage (50-wallet alert sweep ≈ $0.0002 per wallet).

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'get_liquidation_risk',
    'Liquidation Risk',
    10000,
    'Tiered DeFi liquidation risk across Aave v3 and Compound v3 for up to 50 wallets per call'
)
ON CONFLICT (tool_name) DO NOTHING;
