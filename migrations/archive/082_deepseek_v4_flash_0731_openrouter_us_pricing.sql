-- Seed pricing for DeepSeek V4 Flash 0731 (OpenRouter / US).
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- Provider list price: $0.09000000/M input, $0.18000000/M output.
-- Teardrop rate (+25% margin): 113 input, 225 output atomic USDC per 1k tokens.
-- The retired model is deprecated and no longer selected by production routing.
-- usage_events store immutable cost snapshots, so this does not alter billing history.

DELETE FROM pricing_rules WHERE id = 'openrouter-deepseek-v4-flash-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-v4-flash-0731-v1',
     'DeepSeek V4 Flash 0731 (OpenRouter / US)',
     'openrouter', 'deepseek/deepseek-v4-flash-0731',
     10000, 113, 225, 500, NOW())

ON CONFLICT (id) DO NOTHING;
