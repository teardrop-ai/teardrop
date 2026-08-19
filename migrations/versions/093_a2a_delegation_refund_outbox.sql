-- Migration 093: durable A2A delegation refund state
-- Domain: billing / delegation
-- Invariant: every funded delegation has one idempotent refund/cancel record.

CREATE TABLE IF NOT EXISTS a2a_delegation_refund_outbox (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL,
    amount_usdc BIGINT NOT NULL CHECK (amount_usdc > 0),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'refund_requested', 'refunded', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CHECK (
        (status IN ('pending', 'refund_requested') AND resolved_at IS NULL)
        OR (status IN ('refunded', 'cancelled') AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_refund_outbox_pending
    ON a2a_delegation_refund_outbox (status, created_at)
    WHERE status = 'refund_requested';