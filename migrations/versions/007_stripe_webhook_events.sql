-- Migration 007: Stripe webhook events (idempotency + audit)
-- Stores each processed checkout.session.completed event exactly once.
-- The PRIMARY KEY on stripe_event_id is the idempotency guard:
-- duplicate webhook deliveries → ON CONFLICT DO NOTHING → no double-topup.

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    stripe_event_id  TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    amount_usdc      BIGINT NOT NULL CHECK (amount_usdc > 0),
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_org
    ON stripe_webhook_events (org_id, processed_at DESC);
