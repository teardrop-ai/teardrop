-- Add provider cache-token telemetry columns for usage benchmarking.

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_usage_org_created_cache
    ON usage_events (org_id, created_at DESC)
    WHERE (cache_read_tokens > 0 OR cache_creation_tokens > 0);
