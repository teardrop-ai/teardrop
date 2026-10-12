-- Migration 126: partial index for outstanding credit-settlement obligations.
-- Domain: billing
-- Invariant: additive. Supports the verify_credit admission query, which sums
-- an org's unpaid credit charges (pending, retrying, or exhausted within the
-- 7-day re-arm window) and subtracts them from the available balance. No rows
-- are rewritten; amounts remain BIGINT atomic USDC.

CREATE INDEX IF NOT EXISTS idx_pending_settlements_credit_outstanding
    ON pending_settlements (org_id, created_at)
    WHERE billing_method = 'credit'
      AND status IN ('pending', 'retrying', 'exhausted');
