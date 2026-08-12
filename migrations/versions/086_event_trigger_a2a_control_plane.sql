-- Migration 086: A2A event-trigger task lookup and immutable operational audit.
-- Domain: A2A / scheduling
-- Invariant: Additive schema only; audit rows survive trigger deletion.

CREATE INDEX IF NOT EXISTS idx_scheduled_run_results_org_schedule_run
    ON scheduled_run_results (org_id, schedule_id, run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS event_trigger_events (
    id          TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    org_id      TEXT NOT NULL,
    run_id      TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL CHECK (event_type IN (
        'trigger_created',
        'trigger_updated',
        'trigger_deleted',
        'secret_rotated',
        'secret_rejected',
        'dispatch_accepted',
        'dispatch_duplicate',
        'run_settled'
    )),
    status      TEXT NOT NULL DEFAULT '',
    cost_usdc   BIGINT NOT NULL DEFAULT 0 CHECK (cost_usdc >= 0),
    error       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_trigger_events_org_created
    ON event_trigger_events (org_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_event_trigger_events_schedule_created
    ON event_trigger_events (schedule_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_event_trigger_events_run
    ON event_trigger_events (run_id)
    WHERE run_id <> '';
