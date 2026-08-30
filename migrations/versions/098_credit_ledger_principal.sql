-- Migration 098: attribute credit ledger debits to an authenticated principal
-- Domain: billing
-- Invariant: Additive column only; principal attribution never changes debit amounts or ledger balances
--
-- Records which authenticated principal (user or M2M credential) caused each
-- credit ledger debit, enabling per-principal spend reporting. The partial
-- index backs per-principal 24h spend aggregates without touching the
-- append-only audit trail.

ALTER TABLE org_credit_ledger
    ADD COLUMN IF NOT EXISTS principal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_credit_ledger_principal_debits
    ON org_credit_ledger (org_id, principal_id, created_at DESC)
    WHERE operation = 'debit' AND principal_id IS NOT NULL;