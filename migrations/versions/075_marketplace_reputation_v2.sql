-- Migration 075: recency-aware marketplace reputation diagnostics
-- Derived telemetry only: this has no effect on settlement, author earnings,
-- tool pricing, or the immutable financial ledgers.

ALTER TABLE marketplace_tool_call_stats
    ADD COLUMN IF NOT EXISTS reputation_sample_size NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reputation_confidence NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reputation_freshness NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reputation_task_success JSONB NOT NULL DEFAULT '{}'::JSONB;