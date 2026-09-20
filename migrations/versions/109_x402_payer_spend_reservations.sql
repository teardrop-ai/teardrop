-- Migration 109: attach anonymous payer spend reservations to x402 replay claims.
-- Domain: x402 / MCP billing
-- Invariant: one payment nonce reserves at most one non-negative atomic-USDC amount.

ALTER TABLE x402_payment_nonces
    ADD COLUMN IF NOT EXISTS payer_address TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS reserved_cost_usdc BIGINT NOT NULL DEFAULT 0
        CHECK (reserved_cost_usdc >= 0);

CREATE INDEX IF NOT EXISTS idx_x402_payment_nonces_payer_claimed
    ON x402_payment_nonces (LOWER(payer_address), claimed_at)
    WHERE reserved_cost_usdc > 0;

COMMENT ON COLUMN x402_payment_nonces.payer_address IS
    'Verified payer identity used only for bounded anonymous x402 spend enforcement.';
COMMENT ON COLUMN x402_payment_nonces.reserved_cost_usdc IS
    'Atomic USDC admitted against the payer rolling cap; retained with the replay claim for 24 hours.';
