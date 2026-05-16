-- Narrow index for 24h debit-spend aggregates used by billing limits.
CREATE INDEX IF NOT EXISTS idx_credit_ledger_debit_time
    ON org_credit_ledger (org_id, created_at DESC)
    WHERE operation = 'debit';
