-- Seed pricing rules for the new shared-pool models (April 2026 pool refresh).
-- Rates in atomic USDC (1_000_000 = $1.00). All costs include ~25% margin over
-- provider list price.
--
-- DeepSeek V3.2 via OpenRouter / DeepInfra (US, SOC 2):
--   Provider list: ~$0.14/M input, ~$0.28/M output
--   Teardrop rate: $0.175/M input = 175 atomic, $0.35/M output = 350 atomic
--
-- Gemini 3 Flash Preview:
--   Provider list: ~$0.10/M input, ~$0.40/M output
--   Teardrop rate: $0.125/M input = 125 atomic, $0.50/M output = 500 atomic
--
-- Claude Sonnet 4.6:
--   Provider list: $3.00/M input, $15.00/M output
--   Teardrop rate: $3.75/M input = 3750 atomic, $18.75/M output = 18750 atomic
--
-- ON CONFLICT DO NOTHING makes this idempotent on re-run.

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-v3-2-v1',
     'DeepSeek V3.2 (OpenRouter / DeepInfra)',
     'openrouter', 'deepseek/deepseek-v3.2',
     10000, 175, 350, 500, NOW()),

    ('google-gemini-3-flash-preview-v1',
     'Gemini 3 Flash Preview',
     'google', 'gemini-3-flash-preview',
     10000, 125, 500, 500, NOW()),

    ('anthropic-sonnet-4-6-v1',
     'Claude Sonnet 4.6',
     'anthropic', 'claude-sonnet-4-6',
     10000, 3750, 18750, 1000, NOW())

ON CONFLICT (id) DO NOTHING;
