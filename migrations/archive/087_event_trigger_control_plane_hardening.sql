-- Migration 087: multi-instance event-trigger execution leases.
-- Domain: A2A / scheduling
-- Invariant: additive schema; financial truth remains in existing ledgers.

CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_run_results_schedule_run
    ON scheduled_run_results (schedule_id, run_id);

CREATE TABLE IF NOT EXISTS event_dispatch_leases (
    run_id           TEXT PRIMARY KEY,
    schedule_id      TEXT NOT NULL,
    org_id           TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN (
        'reserved',
        'running',
        'completed',
        'failed'
    )),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    attempts         INTEGER NOT NULL DEFAULT 1 CHECK (attempts > 0),
    last_error       TEXT NOT NULL DEFAULT '' CHECK (char_length(last_error) <= 1024),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT event_dispatch_leases_dispatch_fk
        FOREIGN KEY (schedule_id, idempotency_key)
        REFERENCES event_dispatch_keys (schedule_id, idempotency_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_dispatch_leases_active
    ON event_dispatch_leases (lease_expires_at, run_id)
    WHERE status IN ('reserved', 'running');

CREATE INDEX IF NOT EXISTS idx_event_dispatch_leases_org_active
    ON event_dispatch_leases (org_id, lease_expires_at, run_id)
    WHERE status IN ('reserved', 'running');

CREATE INDEX IF NOT EXISTS idx_event_dispatch_leases_terminal_created
    ON event_dispatch_leases (created_at, run_id)
    WHERE status IN ('completed', 'failed');
