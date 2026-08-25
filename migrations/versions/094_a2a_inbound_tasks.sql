-- Migration 094: asynchronous inbound A2A task state.
-- Domain: A2A / agent execution
-- Invariant: transient task state is separate from the immutable inbound audit ledger.

CREATE TABLE IF NOT EXISTS a2a_inbound_tasks (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL UNIQUE,
    client_task_id     TEXT NOT NULL DEFAULT '',
    context_id         TEXT NOT NULL DEFAULT '',
    message            JSONB NOT NULL,
    metadata           JSONB NOT NULL DEFAULT '{}'::JSONB,
    user_message       TEXT NOT NULL,
    caller_org_id      TEXT NOT NULL DEFAULT '',
    caller_user_id     TEXT NOT NULL DEFAULT '',
    caller_ip          TEXT NOT NULL DEFAULT '',
    auth_method        TEXT NOT NULL DEFAULT 'anonymous',
    task_state         TEXT NOT NULL DEFAULT 'submitted'
                       CHECK (task_state IN (
                           'submitted',
                           'running',
                           'completed',
                           'failed',
                           'timeout',
                           'rejected_payment',
                           'rejected_auth_credit'
                       )),
    output_text        TEXT NOT NULL DEFAULT ''
                       CHECK (char_length(output_text) <= 65536),
    error              TEXT NOT NULL DEFAULT ''
                       CHECK (char_length(error) <= 1024),
    usage_event_id     TEXT,
    cost_usdc          BIGINT NOT NULL DEFAULT 0 CHECK (cost_usdc >= 0),
    settlement_tx      TEXT NOT NULL DEFAULT '',
    billing_method     TEXT NOT NULL DEFAULT '',
    duration_ms        INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (char_length(client_task_id) <= 256),
    CHECK (char_length(context_id) <= 256),
    CHECK (
        (task_state IN ('submitted', 'running') AND finished_at IS NULL)
        OR (task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit')
            AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_caller
    ON a2a_inbound_tasks (caller_org_id, caller_user_id, created_at DESC, id DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_auth_client_id
    ON a2a_inbound_tasks (caller_org_id, caller_user_id, client_task_id)
    WHERE client_task_id <> '' AND caller_org_id <> '' AND caller_user_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_anonymous_client_id
    ON a2a_inbound_tasks (caller_ip, client_task_id)
    WHERE client_task_id <> '' AND caller_org_id = '' AND caller_user_id = '';

CREATE INDEX IF NOT EXISTS idx_a2a_inbound_tasks_terminal_created
    ON a2a_inbound_tasks (created_at, id)
    WHERE task_state IN ('completed', 'failed', 'timeout', 'rejected_payment', 'rejected_auth_credit');
