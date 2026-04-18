-- 028: Marketplace subscriptions — let orgs subscribe to marketplace tools
-- for automatic injection into /agent/run.
--
-- Subscriptions use qualified names (e.g. "acme/weather") so they survive
-- tool unpublish/republish cycles and are human-readable.

CREATE TABLE IF NOT EXISTS org_marketplace_subscriptions (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    org_id              TEXT NOT NULL REFERENCES orgs(id),
    qualified_tool_name TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    subscribed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(org_id, qualified_tool_name)
);

CREATE INDEX IF NOT EXISTS idx_mp_subs_org
    ON org_marketplace_subscriptions(org_id)
    WHERE is_active = TRUE;

COMMENT ON TABLE org_marketplace_subscriptions IS
    'Per-org subscriptions to marketplace tools. Subscribed tools are injected into /agent/run.';
