-- Correct Claude Haiku 4.5 pricing seed (migration 022 used Haiku 3 rates).
-- Anthropic list price: $1.00/M input, $5.00/M output.
-- Teardrop rate (+25% margin): $1.25/M input = 1250 atomic, $6.25/M output = 6250 atomic.
-- Idempotent: WHERE guard prevents double-apply.
-- Does NOT touch usage_events — historical cost_usdc rows are immutable snapshots.

UPDATE pricing_rules
SET
    tokens_in_cost_per_1k  = 1250,
    tokens_out_cost_per_1k = 6250
WHERE id = 'anthropic-haiku-v1'
  AND tokens_in_cost_per_1k = 313;
