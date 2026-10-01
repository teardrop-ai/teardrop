-- Migration 117: register assess_market_resolution composite platform tool
-- Domain: marketplace / tools / prediction markets
-- Invariant: atomic USDC values are BIGINT (25000 atomic = $0.025 USDC); idempotent insert.

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
    'assess_market_resolution',
    'Assess Market Resolution',
    25000,
    'Composite Polymarket resolution-risk verdict: rule wording, UMA dispute state, and executability',
    'data',
    ARRAY['prediction-markets', 'polymarket', 'resolution', 'risk', 'uma'],
    'Checks whether a Polymarket market is well-defined and safe to trade before an agent takes a position'
)
ON CONFLICT (tool_name) DO NOTHING;
