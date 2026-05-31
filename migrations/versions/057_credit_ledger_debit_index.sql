-- Narrow index for 24h debit-spend aggregates used by billing limits.
-- Domain: billing
-- Invariant: Index supporting 24h rolling spend computation; no data mutation
CREATE INDEX IF NOT EXISTS idx_credit_ledger_debit_time
    ON org_credit_ledger (org_id, created_at DESC)
    WHERE operation = 'debit';
