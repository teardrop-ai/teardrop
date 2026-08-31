-- Seed pricing for Claude Sonnet 5.
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- Provider list price: $2.000000/M input, $10.00000/M output.
-- Teardrop rate (+25% margin): 2500 input, 12500 output atomic USDC per 1k tokens.

DELETE FROM pricing_rules WHERE id = 'anthropic-sonnet-4-6-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('anthropic-claude-sonnet-5-v1',
     'Claude Sonnet 5',
     'anthropic', 'claude-sonnet-5',
     10000, 2500, 12500, 1000, NOW())

ON CONFLICT (id) DO NOTHING;
