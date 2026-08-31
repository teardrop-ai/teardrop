-- Migration 103: link credit-funded A2A refunds to immutable debit/reversal ledger entries
-- Domain: billing / delegation
-- Invariant: at most one reversal per debit; refunds are append-only top-ups that never rewrite
-- the immutable credit ledger; principal attribution is derived from the original debit row.
--
-- New delegation funding rows store the debit ledger ID in the refund outbox.
-- A completed refund is an immutable top-up linked to that debit by
-- reverses_ledger_id, so rolling org and principal 24h spend aggregates exclude
-- the reversed debit and refunded cap headroom returns. Pre-upgrade refund rows
-- without a debit link retain balance-only refund behavior.

ALTER TABLE org_credit_ledger
    ADD COLUMN IF NOT EXISTS reverses_ledger_id TEXT REFERENCES org_credit_ledger(id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_org_credit_ledger_reversal
    ON org_credit_ledger (reverses_ledger_id)
    WHERE reverses_ledger_id IS NOT NULL;

ALTER TABLE a2a_delegation_refund_outbox
    ADD COLUMN IF NOT EXISTS debit_ledger_id TEXT REFERENCES org_credit_ledger(id);

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_refund_outbox_debit
    ON a2a_delegation_refund_outbox (debit_ledger_id)
    WHERE debit_ledger_id IS NOT NULL;

INSERT INTO marketplace_platform_tools (tool_name, display_name, base_price_usdc, description)
VALUES (
    'discover_agents',
    'A2A Agent Discovery',
    0,
    'Read-only discovery of opt-in remote A2A agents and public reputation status'
)
ON CONFLICT (tool_name) DO NOTHING;