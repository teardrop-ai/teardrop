-- Separate attempt metrics from billable/failure metrics in usage accounting.
ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS billable_tool_calls INTEGER NOT NULL DEFAULT 0;

ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS billable_tool_names TEXT NOT NULL DEFAULT '[]';

ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS failed_tool_calls INTEGER NOT NULL DEFAULT 0;

ALTER TABLE usage_events
ADD COLUMN IF NOT EXISTS failed_tool_names TEXT NOT NULL DEFAULT '[]';
