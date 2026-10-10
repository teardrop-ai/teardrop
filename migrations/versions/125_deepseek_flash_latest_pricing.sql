-- Migration 125: seed pricing for the ~deepseek/deepseek-flash-latest alias
-- (replaces deepseek-v4-flash-0731 as the default / cost-tier model).
-- Domain: billing
-- Invariant: additive. Per-1k-token rates in BIGINT atomic USDC.
-- The alias target changes over time, so the rule is priced at the ceiling of
-- the current DeepSeek Flash family on OpenRouter (+25% margin), not today's
-- alias price ($0.0001/M in, $0.60/M out):
--   input  $0.30/M (v4.1-flash) -> 375
--   output $1.28/M (v4-flash)   -> 1600
-- With openrouter_actual_cost_billing_enabled, turns bill on reported cost instead.

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-flash-latest-v1',
     'DeepSeek Flash latest (OpenRouter alias)',
     'openrouter', '~deepseek/deepseek-flash-latest',
     10000, 375, 1600, 500, NOW())
ON CONFLICT (id) DO NOTHING;
