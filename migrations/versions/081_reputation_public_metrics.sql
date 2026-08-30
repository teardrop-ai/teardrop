-- Migration 081: public marketplace reputation metrics
-- Domain: marketplace / reputation
-- Invariant: Derived telemetry only; CHECK constraints bound public metrics; financial ledgers unchanged
-- Derived telemetry only; financial ledgers and settlement paths are unchanged.

ALTER TABLE marketplace_tool_call_stats
    ADD COLUMN IF NOT EXISTS success_rate NUMERIC NOT NULL DEFAULT 0
        CHECK (success_rate >= 0 AND success_rate <= 1),
    ADD COLUMN IF NOT EXISTS average_latency_ms NUMERIC NOT NULL DEFAULT 0
        CHECK (average_latency_ms >= 0),
    ADD COLUMN IF NOT EXISTS unique_caller_count BIGINT NOT NULL DEFAULT 0
        CHECK (unique_caller_count >= 0);

CREATE INDEX IF NOT EXISTS idx_tool_call_events_tool_org
    ON tool_call_events (tool_name, org_id);
