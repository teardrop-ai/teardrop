-- Seed GPT-5.4 mini pricing for synthesis-turn cost attribution.
-- OpenAI list price: $0.75/M input, $4.50/M output.
-- Teardrop rate (+25% margin): $0.9375/M input, $5.625/M output.
-- Stored in atomic USDC per 1k tokens:
--   input  = 938   (rounded from 937.5)
--   output = 5625

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openai-gpt54-mini-v1',
     'GPT-5.4 mini',
     'openai', 'gpt-5.4-mini',
     10000, 938, 5625, 1000, NOW())

ON CONFLICT (id) DO NOTHING;
