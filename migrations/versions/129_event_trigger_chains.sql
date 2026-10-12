-- Migration 129: at-most-once event-trigger chains with a configurable hop cap.
-- Domain: A2A / scheduling
-- Invariant: additive schema; lineage is stored once per dispatch and chains stay linear.
-- Hop policy is enforced by EVENT_CHAIN_MAX_HOPS; the database enforces lineage integrity.

ALTER TABLE scheduled_runs
    ADD COLUMN IF NOT EXISTS next_trigger_id TEXT REFERENCES scheduled_runs(id) ON DELETE SET NULL;
ALTER TABLE scheduled_runs DROP CONSTRAINT IF EXISTS scheduled_runs_next_trigger_chk;
ALTER TABLE scheduled_runs
    ADD CONSTRAINT scheduled_runs_next_trigger_chk
    CHECK (next_trigger_id IS NULL OR schedule_kind = 'event');

ALTER TABLE event_dispatch_keys
    ADD COLUMN IF NOT EXISTS chain_id TEXT,
    ADD COLUMN IF NOT EXISTS hop_index SMALLINT NOT NULL DEFAULT 0;
UPDATE event_dispatch_keys SET chain_id = run_id WHERE chain_id IS NULL;
ALTER TABLE event_dispatch_keys ALTER COLUMN chain_id SET NOT NULL;
ALTER TABLE event_dispatch_keys DROP CONSTRAINT IF EXISTS event_dispatch_keys_hop_chk;
ALTER TABLE event_dispatch_keys
    ADD CONSTRAINT event_dispatch_keys_hop_chk
    CHECK (hop_index >= 0 AND (hop_index > 0 OR chain_id = run_id));
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_dispatch_keys_chain_hop
    ON event_dispatch_keys (chain_id, hop_index);

ALTER TABLE event_trigger_events DROP CONSTRAINT IF EXISTS event_trigger_events_event_type_check;
ALTER TABLE event_trigger_events DROP CONSTRAINT IF EXISTS event_trigger_events_event_type_chk;
ALTER TABLE event_trigger_events
    ADD CONSTRAINT event_trigger_events_event_type_chk
    CHECK (event_type IN (
        'trigger_created',
        'trigger_updated',
        'trigger_deleted',
        'secret_rotated',
        'secret_rejected',
        'dispatch_accepted',
        'dispatch_duplicate',
        'run_settled',
        'chain_dispatch_skipped'
    ));
