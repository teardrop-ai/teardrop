-- Migration 077: retention index for telemetry completeness denominators
-- telemetry_run_starts is non-financial and retained only for the configured
-- completeness-reporting window. This index supports ordered, batched cleanup.

CREATE INDEX IF NOT EXISTS idx_telemetry_run_starts_started_at
    ON telemetry_run_starts (started_at);