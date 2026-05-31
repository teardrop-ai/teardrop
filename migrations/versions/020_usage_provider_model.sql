-- Track which provider/model was used for each agent run.
-- Domain: billing
-- Invariant: Records provider/model per run for accurate per-model pricing

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_usage_events_provider_model
    ON usage_events (provider, model);
