-- Migration 008: USDC on-chain top-up events (idempotency + audit)
-- Stores each settled USDC top-up transaction exactly once.
-- The PRIMARY KEY on tx_hash is the idempotency guard:
-- duplicate submissions of the same on-chain tx → ON CONFLICT DO NOTHING → no double-credit.

CREATE TABLE IF NOT EXISTS usdc_topup_events (
    tx_hash      TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc  BIGINT NOT NULL CHECK (amount_usdc > 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usdc_topup_events_org
    ON usdc_topup_events (org_id, processed_at DESC);
