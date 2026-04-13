-- Migration 014: org spending limits + admin pause
-- Adds spending_limit_usdc (24h rolling window cap) and is_paused flag
-- to org_credits. Default values preserve existing behaviour.

ALTER TABLE org_credits
    ADD COLUMN IF NOT EXISTS spending_limit_usdc BIGINT NOT NULL DEFAULT 0;
    -- 0 = unlimited (no cap enforced)

ALTER TABLE org_credits
    ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT FALSE;
