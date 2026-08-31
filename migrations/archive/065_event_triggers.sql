-- 065_event_triggers.sql
-- Adds reactive (event-triggered) runs on top of the scheduled_runs subsystem.
-- Additive + backward-compatible: loosens NOT NULL on interval-only columns so a
-- single table can hold both interval schedules and event triggers. The polling
-- worker already filters schedule_kind = 'interval', so event rows are inert to it.
--
-- Invariant: per-trigger signing secrets are stored ONLY as SHA-256 hashes; the
-- plaintext secret is returned once at creation/rotation (mirrors migration 005).

-- 1. Allow the 'event' schedule kind.
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_kind_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_kind_chk CHECK (schedule_kind IN ('interval', 'event'));

-- 2. Interval-only columns become optional for event rows.
ALTER TABLE scheduled_runs ALTER COLUMN interval_seconds DROP NOT NULL;
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_interval_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_interval_chk CHECK (interval_seconds IS NULL OR interval_seconds > 0);
ALTER TABLE scheduled_runs ALTER COLUMN next_run_at DROP NOT NULL;

-- 3. Event-trigger routing + auth columns.
ALTER TABLE scheduled_runs ADD COLUMN IF NOT EXISTS trigger_token TEXT;
ALTER TABLE scheduled_runs ADD COLUMN IF NOT EXISTS secret_hash TEXT;

-- Public, non-secret routing id used in the inbound URL. Unique among event rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_runs_trigger_token
    ON scheduled_runs (trigger_token)
    WHERE trigger_token IS NOT NULL;

-- An event row must carry both a routing token and a secret hash.
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_event_shape_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_event_shape_chk CHECK (
        schedule_kind <> 'event'
        OR (trigger_token IS NOT NULL AND secret_hash IS NOT NULL)
    );

-- 4. Idempotency / at-most-once reservation for inbound dispatches.
-- Append-only; insert-first reservation guarantees a given (trigger, key) runs once.
CREATE TABLE IF NOT EXISTS event_dispatch_keys (
    schedule_id TEXT NOT NULL REFERENCES scheduled_runs(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (schedule_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_event_dispatch_keys_created_at
    ON event_dispatch_keys (created_at DESC);
