-- Migration 013: pending settlements retry queue
-- Stores failed settlements for asynchronous retry with exponential backoff.
-- Both x402 on-chain and credit debit failures are enqueued here.

CREATE TABLE IF NOT EXISTS pending_settlements (
    id                TEXT PRIMARY KEY,
    usage_event_id    TEXT NOT NULL,
    org_id            TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    billing_method    TEXT NOT NULL CHECK (billing_method IN ('x402', 'credit')),
    amount_usdc       BIGINT NOT NULL DEFAULT 0,
    payment_payload   TEXT,           -- base64-encoded for x402; NULL for credit
    retry_count       INT NOT NULL DEFAULT 0,
    max_retries       INT NOT NULL DEFAULT 5,
    next_retry_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'retrying', 'settled', 'exhausted')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The retry worker queries this index every N seconds.
CREATE INDEX IF NOT EXISTS idx_pending_settlements_retry
    ON pending_settlements (next_retry_at)
    WHERE status IN ('pending', 'retrying');

-- Admin reconciliation: quickly find exhausted items.
CREATE INDEX IF NOT EXISTS idx_pending_settlements_status
    ON pending_settlements (status, created_at DESC);
