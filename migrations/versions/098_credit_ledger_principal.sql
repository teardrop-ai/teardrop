-- 098: Attribute credit ledger debits to an authenticated principal.

ALTER TABLE org_credit_ledger
    ADD COLUMN IF NOT EXISTS principal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_credit_ledger_principal_debits
    ON org_credit_ledger (org_id, principal_id, created_at DESC)
    WHERE operation = 'debit' AND principal_id IS NOT NULL;