-- Migration 123: seed pricing for Gemini 3.8 Flash (replaces 3.6 Flash as the
-- google default/speed-tier model).
-- Domain: billing
-- Invariant: additive. Per-1k-token rates in BIGINT atomic USDC, mirroring the
-- 3.6 Flash rule (1875 input, 9375 output) so the swap never lowers the charge.
-- The 3.6 rule is kept for historical runs and pinned org configs.

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('google-gemini-3-8-flash-v1',
     'Gemini 3.8 Flash',
     'google', 'gemini-3.8-flash',
     10000, 1875, 9375, 1000, NOW())
ON CONFLICT (id) DO NOTHING;
