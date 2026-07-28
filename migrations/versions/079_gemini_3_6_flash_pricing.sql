-- Seed pricing for Gemini 3.6 Flash.
-- Domain: billing
-- Invariant: Per-1k-token rates in BIGINT atomic USDC
-- Provider list price: $1.5000000/M input, $7.5000000/M output.
-- Teardrop rate (+25% margin): 1875 input, 9375 output atomic USDC per 1k tokens.

DELETE FROM pricing_rules WHERE id = 'google-gemini-3-flash-preview-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('google-gemini-3-6-flash-v1',
     'Gemini 3.6 Flash',
     'google', 'gemini-3.6-flash',
     10000, 1875, 9375, 1000, NOW())

ON CONFLICT (id) DO NOTHING;
