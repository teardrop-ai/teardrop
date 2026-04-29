-- Migration 044: Correct Gemini 3 Flash Preview pricing.
--
-- Migration 039 seeded this model using Gemini 2.0 Flash provider rates
-- (~$0.10/M input, ~$0.40/M output), but Gemini 3 Flash Preview is priced
-- materially higher by Google:
--
--   Google list price (as of 2026-04-28):
--     Input:  $0.50/M tokens  (text / image / video)
--     Output: $3.00/M tokens
--
--   Teardrop rate at ~25% margin:
--     Input:  $0.625/M = 625 atomic USDC per 1k tokens
--     Output: $3.750/M = 3750 atomic USDC per 1k tokens
--
-- The previous row (id = 'google-gemini-3-flash-preview-v1') had:
--     tokens_in_cost_per_1k  = 125   ($0.125/M — 75% below provider cost)
--     tokens_out_cost_per_1k = 500   ($0.500/M — 83% below provider cost)
--
-- UPDATE is used (not INSERT … ON CONFLICT DO NOTHING) so that the corrected
-- rates take effect immediately without requiring a row delete + re-seed.

UPDATE pricing_rules
SET
    name                   = 'Gemini 3 Flash Preview (corrected 2026-04-28)',
    tokens_in_cost_per_1k  = 625,
    tokens_out_cost_per_1k = 3750,
    effective_from         = NOW()
WHERE id = 'google-gemini-3-flash-preview-v1';
