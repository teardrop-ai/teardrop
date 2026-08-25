-- Migration 096: durable A2A delivery ambiguity state
-- Domain: A2A / billing / x402
-- Invariant: ambiguous paid sends are held for explicit reconciliation; the
-- immutable delegation event ledger remains append-only.

ALTER TABLE a2a_delegation_refund_outbox
    ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'not_attempted'
        CHECK (delivery_status IN ('not_attempted', 'possibly_delivered', 'confirmed', 'failed')),
    ADD COLUMN IF NOT EXISTS delivery_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_resolved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_settlement_tx TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS delivery_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_refund_outbox_delivery_review
    ON a2a_delegation_refund_outbox (org_id, created_at DESC)
    WHERE delivery_status = 'possibly_delivered';

COMMENT ON COLUMN a2a_delegation_refund_outbox.delivery_status IS
    'Mutable delivery projection; possibly_delivered requires operator reconciliation before refund.';
COMMENT ON COLUMN a2a_delegation_refund_outbox.delivery_settlement_tx IS
    'Validated bounded x402 settlement transaction identifier, when supplied by the remote response.';
