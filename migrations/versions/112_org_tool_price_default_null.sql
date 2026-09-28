-- Migration 112: tri-state org tool pricing.
-- Domain: marketplace / billing
-- Invariant: base_price_usdc NULL = platform default (pricing_rules.tool_call_cost), 0 = free, > 0 = author price.
-- The backfill runs only while the column is still NOT NULL, so re-runs never erase an explicit 0.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'org_tools'
          AND column_name = 'base_price_usdc'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE org_tools ALTER COLUMN base_price_usdc DROP NOT NULL;
        ALTER TABLE org_tools ALTER COLUMN base_price_usdc DROP DEFAULT;
        UPDATE org_tools SET base_price_usdc = NULL WHERE base_price_usdc = 0;
    END IF;
END $$;

COMMENT ON COLUMN org_tools.base_price_usdc IS
    'Author per-call price in atomic USDC (6 decimals). NULL = platform default; 0 = free.';
