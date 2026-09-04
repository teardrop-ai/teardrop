-- Migration 105: register validate_opportunity composite platform tool
-- Domain: marketplace / tools / defi
-- Invariant: atomic USDC values are BIGINT (15000 atomic = $0.015 USDC); idempotent insert.

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
    'validate_opportunity',
    'Validate Opportunity',
    15000,
    'Composite DeFi yield pool sustainability verdict and risk factor analysis',
    'defi',
    ARRAY['defi', 'yield', 'risk', 'validation', 'opportunity'],
    'Validates DeFi yield pool sustainability and risks before capital deployment'
)
ON CONFLICT (tool_name) DO NOTHING;
