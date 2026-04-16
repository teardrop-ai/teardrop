-- Seed model-specific pricing rules (Teardrop shared-key rates with ~25% margin).
-- Rates in atomic USDC (6 decimals). E.g. 313 = $0.000313 per 1k tokens.

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('anthropic-haiku-v1', 'Claude Haiku 4.5', 'anthropic', 'claude-haiku-4-5-20251001',
     10000, 313, 1563, 1000, NOW()),
    ('openai-gpt4o-mini-v1', 'GPT-4o Mini', 'openai', 'gpt-4o-mini',
     10000, 188, 750, 1000, NOW()),
    ('google-flash-v1', 'Gemini 2.0 Flash', 'google', 'gemini-2.0-flash',
     10000, 94, 375, 1000, NOW())
ON CONFLICT (id) DO NOTHING;
