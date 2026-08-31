-- 070: Bounded task-type telemetry for outbound A2A delegation events.
-- Domain: A2A / non-financial analytics
-- Invariant: This additive field must not affect credit debits, x402 settlement,
-- or the append-only financial audit trail.

ALTER TABLE a2a_delegation_events
    ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT 'general'
    CHECK (task_type IN ('general', 'research', 'analysis', 'data_retrieval', 'coding', 'transaction', 'automation'));

CREATE INDEX IF NOT EXISTS idx_a2a_delegation_events_org_task_type_created
    ON a2a_delegation_events (org_id, task_type, created_at DESC);

COMMENT ON COLUMN a2a_delegation_events.task_type IS
    'Bounded delegation task class. Never contains raw task descriptions or credentials.';