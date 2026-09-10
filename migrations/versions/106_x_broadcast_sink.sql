-- Migration 106: add X (Twitter) broadcast sink and audit ledger
-- Domain: scheduling / publishing
-- Invariant: X broadcast publishes at most once per completed run; audit records are immutable;
-- callback_format 'x' forbids callback_url.

DO $$
BEGIN
    ALTER TABLE scheduled_runs
        DROP CONSTRAINT IF EXISTS scheduled_runs_callback_format_chk;

    ALTER TABLE scheduled_runs
        ADD CONSTRAINT scheduled_runs_callback_format_chk
        CHECK (callback_format IN ('json', 'text', 'x'));
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'scheduled_runs_x_sink_chk'
    ) THEN
        ALTER TABLE scheduled_runs
            ADD CONSTRAINT scheduled_runs_x_sink_chk
            CHECK (callback_format <> 'x' OR callback_url IS NULL);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS x_broadcasts (
    run_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    tweet_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_x_broadcasts_org_created_at
    ON x_broadcasts (org_id, created_at DESC);

COMMENT ON TABLE x_broadcasts IS 'Durable audit ledger of scheduled runs broadcast to X (Twitter).';
COMMENT ON COLUMN x_broadcasts.run_id IS 'Run ID corresponding to scheduled_runs execution.';
COMMENT ON COLUMN x_broadcasts.tweet_id IS 'X tweet ID returned after successful publication.';
