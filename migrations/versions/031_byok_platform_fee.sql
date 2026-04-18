-- Migration 031: BYOK platform fee tracking
-- Adds a platform_fee_usdc column to usage_events so the flat per-run fee
-- charged to BYOK orgs is auditable as a separate line item.

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS platform_fee_usdc BIGINT NOT NULL DEFAULT 0;
