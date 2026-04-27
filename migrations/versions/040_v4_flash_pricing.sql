-- Replace DeepSeek V3.2 pricing with V4 Flash (April 2026 cost-tier upgrade).
-- V4 Flash: $0.14/M input, $0.28/M output (provider list) — same Teardrop rates
-- as V3.2 ($0.175/$0.35 per 1M), improving platform margin.
-- No usage_events reference the V3.2 pricing rule (pre-launch), so hard DELETE is safe.

DELETE FROM pricing_rules WHERE id = 'openrouter-deepseek-v3-2-v1';

INSERT INTO pricing_rules
    (id, name, provider, model, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('openrouter-deepseek-v4-flash-v1',
     'DeepSeek V4 Flash (OpenRouter / US)',
     'openrouter', 'deepseek/deepseek-v4-flash',
     10000, 175, 350, 500, NOW())

ON CONFLICT (id) DO NOTHING;
