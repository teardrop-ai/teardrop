-- Migration 092: optional plain-text delivery for scheduled-run callbacks.
-- Domain: scheduling output transport; JSON remains the backward-compatible default.
-- Invariant: callback formatting never changes run execution, billing, or settlement.

ALTER TABLE scheduled_runs
    ADD COLUMN IF NOT EXISTS callback_format TEXT NOT NULL DEFAULT 'json';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'scheduled_runs_callback_format_chk'
    ) THEN
        ALTER TABLE scheduled_runs
            ADD CONSTRAINT scheduled_runs_callback_format_chk
            CHECK (callback_format IN ('json', 'text'));
    END IF;
END $$;
