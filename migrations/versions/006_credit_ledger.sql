-- Migration 006: org credit ledger
-- Adds an immutable audit trail for all credit operations (debits and top-ups).
--
-- Every debit_credit() and admin_topup_credit() call inserts one row.
-- balance_usdc_after captures the post-operation balance for easy reconciliation.

CREATE TABLE IF NOT EXISTS org_credit_ledger (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    operation          TEXT NOT NULL CHECK (operation IN ('debit', 'topup')),
    amount_usdc        BIGINT NOT NULL CHECK (amount_usdc > 0),
    balance_usdc_after BIGINT NOT NULL CHECK (balance_usdc_after >= 0),
    reason             TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_credit_ledger_org_created
    ON org_credit_ledger (org_id, created_at DESC);
