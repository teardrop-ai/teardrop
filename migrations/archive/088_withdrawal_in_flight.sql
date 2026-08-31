-- 088: Durable in-flight settlement state for marketplace withdrawals
-- Domain: marketplace
-- Invariant: claimed earnings must never be re-selected by a later sweep; the claim
-- survives process crashes so a broadcast transfer cannot be double-paid.
--
-- tool_author_earnings.withdrawal_id links a claimed earnings row to its withdrawal,
-- letting reset release ONLY the affected rows instead of a whole org's earnings.
--
-- Status transitions for sweep-initiated withdrawals:
--   pending   → in_flight  (earnings claimed, CDP transfer in progress or ambiguous)
--   in_flight → settled    (transfer confirmed on-chain; manual complete after crash)
--   in_flight → pending    (manual reset after operator confirms NO transfer occurred)
--   in_flight → failed     (transfer definitively rejected/reverted; earnings released)

ALTER TABLE tool_author_earnings
    ADD COLUMN IF NOT EXISTS withdrawal_id TEXT REFERENCES tool_author_withdrawals(id);

CREATE INDEX IF NOT EXISTS idx_author_earnings_withdrawal_id
    ON tool_author_earnings (withdrawal_id)
    WHERE withdrawal_id IS NOT NULL;

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'tool_author_withdrawals'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE tool_author_withdrawals DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END;
$$;

ALTER TABLE tool_author_withdrawals
    ADD CONSTRAINT tool_author_withdrawals_status_check
    CHECK (status IN ('pending', 'in_flight', 'settled', 'failed', 'exhausted'));