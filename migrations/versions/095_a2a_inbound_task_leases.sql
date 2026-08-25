-- Migration 095: multi-instance ownership and settlement projection metadata for inbound A2A tasks.
-- Domain: A2A / agent execution
-- Invariant: task leases coordinate process ownership; financial truth remains in billing ledgers.

ALTER TABLE a2a_inbound_tasks
    ADD COLUMN IF NOT EXISTS worker_owner_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS settlement_amount_usdc BIGINT NOT NULL DEFAULT 0;

ALTER TABLE a2a_inbound_tasks
    ADD CONSTRAINT a2a_inbound_tasks_settlement_amount_nonnegative
    CHECK (settlement_amount_usdc >= 0);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_active_lease
    ON a2a_inbound_tasks (lease_expires_at, id)
    WHERE task_state IN ('submitted', 'running');

ALTER TABLE a2a_inbound_events
    ADD COLUMN IF NOT EXISTS settlement_amount_usdc BIGINT NOT NULL DEFAULT 0;

ALTER TABLE a2a_inbound_events
    ADD CONSTRAINT a2a_inbound_events_settlement_amount_nonnegative
    CHECK (settlement_amount_usdc >= 0);