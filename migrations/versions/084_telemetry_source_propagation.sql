-- Migration 084: propagate run source into ML telemetry rows.
-- Domain: agent telemetry / ML data foundation
-- Invariant: Additive only; never referenced by billing or settlement paths.
-- Legacy rows without a matching usage event retain the safe default source.

ALTER TABLE tool_call_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';

ALTER TABLE tool_call_events
    ADD CONSTRAINT tool_call_events_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'));

ALTER TABLE run_decisions
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';

ALTER TABLE run_decisions
    ADD CONSTRAINT run_decisions_source_chk
    CHECK (source IN ('api', 'schedule', 'trigger', 'a2a'));

-- usage_events is the canonical run dimension. Backfill only rows with a
-- non-empty run ID and a valid source; unmatched legacy rows keep 'api'.
UPDATE tool_call_events AS events
SET source = usage.source
FROM usage_events AS usage
WHERE events.run_id <> ''
  AND events.run_id = usage.run_id
  AND usage.source IN ('api', 'schedule', 'trigger', 'a2a');

UPDATE run_decisions AS decisions
SET source = usage.source
FROM usage_events AS usage
WHERE decisions.run_id <> ''
  AND decisions.run_id = usage.run_id
  AND usage.source IN ('api', 'schedule', 'trigger', 'a2a');

CREATE INDEX IF NOT EXISTS idx_tool_call_events_source_created
    ON tool_call_events (source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_run_decisions_source_created
    ON run_decisions (source, created_at DESC);

COMMENT ON COLUMN tool_call_events.source IS
    'Run origin: api, schedule, trigger, or a2a; copied from usage_events for ML analysis.';

COMMENT ON COLUMN run_decisions.source IS
    'Run origin: api, schedule, trigger, or a2a; copied from usage_events for ML analysis.';
