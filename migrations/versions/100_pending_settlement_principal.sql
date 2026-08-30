-- Migration 100: preserve credit principal attribution across settlement retries
-- Domain: billing
-- Invariant: Additive column; principal attribution survives retry-queue re-enqueue; settlement amounts unchanged
--
-- pending_settlements rows are re-enqueued on failure; carrying principal_id
-- forward keeps per-principal spend attribution intact across retries.

ALTER TABLE pending_settlements
    ADD COLUMN IF NOT EXISTS principal_id TEXT;