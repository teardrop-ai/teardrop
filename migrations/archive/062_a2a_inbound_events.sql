-- Migration 062: inbound A2A audit ledger
-- Domain: A2A
-- Invariant: Inbound A2A requests emit immutable audit rows across accepted and rejected billing outcomes
--
-- Adds a dedicated inbound A2A audit table so operators can inspect caller
-- identity, payment failures, and run outcomes without reconstructing history
-- from usage_events and settlement side effects alone.

CREATE TABLE IF NOT EXISTS a2a_inbound_events (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL DEFAULT '',
    usage_event_id  TEXT,
    caller_org_id   TEXT NOT NULL DEFAULT '',
    caller_user_id  TEXT NOT NULL DEFAULT '',
    caller_address  TEXT NOT NULL DEFAULT '',
    caller_ip       TEXT NOT NULL DEFAULT '',
    auth_method     TEXT NOT NULL DEFAULT '',
    context_id      TEXT NOT NULL DEFAULT '',
    task_id         TEXT NOT NULL DEFAULT '',
    task_state      TEXT NOT NULL
                  CHECK (task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit')),
    cost_usdc       BIGINT NOT NULL DEFAULT 0,
    settlement_tx   TEXT NOT NULL DEFAULT '',
    billing_method  TEXT NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_org
    ON a2a_inbound_events (caller_org_id, created_at DESC)
    WHERE caller_org_id != '';

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_address
    ON a2a_inbound_events (caller_address, created_at DESC)
    WHERE caller_address != '';

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_run
    ON a2a_inbound_events (run_id);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_events_state
    ON a2a_inbound_events (task_state, created_at DESC);