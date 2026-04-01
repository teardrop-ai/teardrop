-- Migration 003: usage-based pricing rule
-- Adds a usage-based pricing rule alongside the existing flat-rate default.
-- Per-unit rates are in atomic USDC (6-decimal integer):
--   1_000_000 = $1.00,  10_000 = $0.01,  1_000 = $0.001
--
-- These starter rates approximate wholesale LLM cost at a small markup:
--   tokens_in_cost_per_1k  = 1500  → $0.0015 per 1k input tokens
--   tokens_out_cost_per_1k = 7500  → $0.0075 per 1k output tokens
--   tool_call_cost         = 1000  → $0.001  per tool invocation
--   run_price_usdc         = 10000 → $0.01   (minimum floor, unused by this rule)
--
-- calculate_run_cost_usdc() uses per-unit rates when they are non-zero;
-- falls back to run_price_usdc for flat-rate rules (where all per-unit = 0).

INSERT INTO pricing_rules (
    id,
    name,
    run_price_usdc,
    tokens_in_cost_per_1k,
    tokens_out_cost_per_1k,
    tool_call_cost,
    effective_from,
    created_at
)
VALUES (
    'usage-based-v1',
    'Usage-based pricing v1',
    10000,    -- $0.01 floor (not charged when per-unit rates apply)
    1500,     -- $0.0015 / 1k input tokens
    7500,     -- $0.0075 / 1k output tokens
    1000,     -- $0.001  / tool call
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;
