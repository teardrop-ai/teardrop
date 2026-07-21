-- Migration 074: deployment provenance for canonical agent-run records
-- Historical rows remain explicitly unknown (empty value); new events are
-- stamped by teardrop.usage from the dependency-free APP_VERSION constant.

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS runner_version TEXT NOT NULL DEFAULT '';