-- Migration 053: enforce zero-cost pricing for in-process utility tools
--
-- These tools do not rely on external paid APIs and should never consume the
-- default per-tool billing rate from pricing_rules.tool_call_cost.

INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
VALUES
    ('calculate', 0, 'Pure in-process arithmetic utility; zero marginal cost'),
    ('get_datetime', 0, 'In-process datetime utility; zero marginal cost'),
    ('count_text_stats', 0, 'In-process text statistics utility; zero marginal cost')
ON CONFLICT (tool_name) DO UPDATE
SET
    cost_usdc = EXCLUDED.cost_usdc,
    description = EXCLUDED.description,
    updated_at = NOW();
