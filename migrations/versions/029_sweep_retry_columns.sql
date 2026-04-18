-- 029: Sweep retry columns — add backoff metadata to tool_author_withdrawals
-- so the background sweep worker can track per-org retry state without a
-- separate dead-letter table.
--
-- sweep_attempt_count  : how many sweep cycles have attempted this withdrawal
-- last_sweep_error     : human-readable reason for the last failure
-- next_sweep_at        : NULL = eligible now; future timestamp = in backoff
--
-- Status transitions for sweep-initiated withdrawals:
--   pending  → settled   (CDP transfer succeeded)
--   pending  → failed    (CDP transfer failed, sweep_attempt_count < max)
--   failed   → pending   (backoff elapsed, eligible for retry)
--   failed   → exhausted (sweep_attempt_count >= max_retries)

ALTER TABLE tool_author_withdrawals
    ADD COLUMN IF NOT EXISTS sweep_attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_sweep_error    TEXT    NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS next_sweep_at       TIMESTAMPTZ;

-- Partial index: only rows eligible for the next sweep cycle need to be scanned.
CREATE INDEX IF NOT EXISTS idx_withdrawals_sweep_eligible
    ON tool_author_withdrawals (next_sweep_at)
    WHERE status IN ('pending', 'failed')
      AND next_sweep_at IS NOT NULL;

-- Status constraint extended to include 'exhausted'.
-- We add it as a new check constraint; existing rows satisfy it because
-- their status is one of the original three values.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tool_author_withdrawals_status_check'
          AND conrelid = 'tool_author_withdrawals'::regclass
    ) THEN
        ALTER TABLE tool_author_withdrawals
            ADD CONSTRAINT tool_author_withdrawals_status_check
            CHECK (status IN ('pending', 'settled', 'failed', 'exhausted'));
    END IF;
END;
$$;
