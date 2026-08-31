-- Migration 076: immutable run-start index for telemetry completeness metrics
-- This is a non-financial observability denominator. It records no prompts,
-- tool arguments, model output, credentials, or payment information.

CREATE TABLE IF NOT EXISTS telemetry_run_starts (
    run_id     TEXT        PRIMARY KEY,
    org_id     TEXT        NOT NULL DEFAULT '',
    source     TEXT        NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT telemetry_run_starts_source_chk
        CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'))
);

CREATE INDEX IF NOT EXISTS idx_telemetry_run_starts_source_started
    ON telemetry_run_starts (source, started_at DESC);