-- Migration 073: data strategy foundations and disposable-data retention support
-- Domain: telemetry / onboarding analytics / operational retention
-- Invariants:
--   * Financial and A2A audit ledgers remain immutable and are never retention targets.
--   * Existing API and SDK contracts remain additive and backward compatible.
--   * Checkpoint cleanup is coordinated through per-thread activity rows.

-- ── Acquisition attribution ─────────────────────────────────────────────────
-- First-touch attribution only. The API accepts a normalized source token; the
-- database constraint protects administrative and future non-HTTP writers too.
ALTER TABLE orgs
    ADD COLUMN IF NOT EXISTS acquisition_source TEXT NOT NULL DEFAULT '';

ALTER TABLE orgs
    ADD CONSTRAINT orgs_acquisition_source_format_chk
    CHECK (acquisition_source ~ '^[a-z0-9_-]{0,64}$');

-- ── Canonical run provenance ─────────────────────────────────────────────────
-- usage_events is the one-record-per-run operational dimension. Keep legacy
-- blank run IDs outside the unique constraint for backwards compatibility.
ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';

ALTER TABLE usage_events
    ADD CONSTRAINT usage_events_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_run_id
    ON usage_events (run_id)
    WHERE run_id <> '';

-- ── ML telemetry compatibility ───────────────────────────────────────────────
ALTER TABLE tool_call_events
    ADD COLUMN IF NOT EXISTS schema_version SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS schema_version SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS taxonomy_version SMALLINT NOT NULL DEFAULT 1;

-- ── Race-safe LangGraph checkpoint retention ─────────────────────────────────
-- The currently installed LangGraph saver owns checkpoint_blobs. Older Teardrop
-- baseline schemas predate that table, so create it here before retention uses it.
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    type          TEXT NOT NULL,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_thread_activity (
    thread_id        TEXT PRIMARY KEY,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Existing threads begin a fresh TTL window. This avoids deriving a timestamp
-- from legacy checkpoint JSON and prevents a migration-time active-run race.
INSERT INTO checkpoint_thread_activity (thread_id, last_activity_at)
SELECT DISTINCT thread_id, NOW()
FROM checkpoints
ON CONFLICT (thread_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_checkpoint_thread_activity_last_activity
    ON checkpoint_thread_activity (last_activity_at);

-- ── Disposable operational records ───────────────────────────────────────────
-- These indexes support bounded retention sweeps. org_tool_events retains
-- create/update/delete audit rows permanently; only execution noise is swept.
CREATE INDEX IF NOT EXISTS idx_scheduled_run_results_created_at
    ON scheduled_run_results (created_at);

CREATE INDEX IF NOT EXISTS idx_org_tool_events_disposable_created_at
    ON org_tool_events (created_at)
    WHERE event_type IN ('executed', 'failed');
