-- Migration 114: index signed x402 delegation deliveries for the treasury outflow cap.
-- Domain: billing / delegation
-- Invariant: delivery_started_at is set exactly when a treasury-signed payment is dispatched; the rolling
-- 24h cap sums amount_usdc over those rows under a transaction-scoped advisory lock.

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_refund_outbox_delivery_started
    ON a2a_delegation_refund_outbox (delivery_started_at)
    WHERE delivery_started_at IS NOT NULL;
