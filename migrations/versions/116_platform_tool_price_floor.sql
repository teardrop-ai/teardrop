-- Migration 116: raise paid platform tool prices to a 5,000 atomic USDC ($0.005) floor.
-- Domain: billing / marketplace
-- Invariants:
--   * Atomic USDC BIGINT; free (0) tools stay free.
--   * Precedence is unchanged (overrides > marketplace price), so both tables are floored.
-- Rationale: a per-settlement facilitator fee (CDP: $0.001) must stay <= 20% of any paid call.
-- Idempotent: rows already at or above the floor are untouched.

UPDATE marketplace_platform_tools
SET base_price_usdc = 5000
WHERE base_price_usdc > 0 AND base_price_usdc < 5000;

UPDATE tool_pricing_overrides
SET cost_usdc = 5000,
    updated_at = NOW()
WHERE cost_usdc > 0 AND cost_usdc < 5000;
