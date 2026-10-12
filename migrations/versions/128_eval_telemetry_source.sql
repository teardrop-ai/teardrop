-- Migration 128: allow source='eval' on run telemetry tables
-- Domain: telemetry / usage
-- Invariant: additive only. Widens the allowed source set on usage_events,
-- telemetry_run_starts, tool_call_events and run_decisions; existing rows stay valid.
-- billing_charges is intentionally untouched: eval is a telemetry label, not a billing source.
-- Deploy before code that writes 'eval' — telemetry writers swallow CHECK violations.

ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_source_chk;
ALTER TABLE usage_events
    ADD CONSTRAINT usage_events_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a', 'eval'));

ALTER TABLE telemetry_run_starts DROP CONSTRAINT IF EXISTS telemetry_run_starts_source_chk;
ALTER TABLE telemetry_run_starts
    ADD CONSTRAINT telemetry_run_starts_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a', 'eval'));

ALTER TABLE tool_call_events DROP CONSTRAINT IF EXISTS tool_call_events_source_chk;
ALTER TABLE tool_call_events
    ADD CONSTRAINT tool_call_events_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a', 'eval'));

ALTER TABLE run_decisions DROP CONSTRAINT IF EXISTS run_decisions_source_chk;
ALTER TABLE run_decisions
    ADD CONSTRAINT run_decisions_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a', 'eval'));
