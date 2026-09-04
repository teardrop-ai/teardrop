-- Migration 104: register assess_counterparty_risk composite platform tool
-- Domain: marketplace / tools / risk
-- Invariant: atomic USDC values are BIGINT (35000 atomic = $0.035 USDC); idempotent insert.

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
    'assess_counterparty_risk',
    'Assess Counterparty Risk',
    35000,
    'Composite counterparty verdict across approvals, liquidation health, and activity history',
    'defi',
    ARRAY['risk', 'counterparty', 'security', 'approvals', 'liquidation'],
    'Assesses EVM counterparty risk across approvals, liquidation health, and activity before sending funds'
)
ON CONFLICT (tool_name) DO NOTHING;
