-- Migration 120: synced OpenRouter model catalogue + alias drift log
-- Domain: models / pricing
-- Invariant: additive only. Rows are upserted by the catalogue sync job and
-- never deleted; models that disappear from the feed get removed_at set.
-- Prices are per-token USD as published by OpenRouter (reference data only;
-- billing still reads pricing_rules).

CREATE TABLE IF NOT EXISTS model_catalogue (
    or_id                  TEXT PRIMARY KEY,
    canonical_slug         TEXT NOT NULL DEFAULT '',
    name                   TEXT NOT NULL DEFAULT '',
    context_length         INTEGER,
    prompt_price_usd       NUMERIC(20, 12),
    completion_price_usd   NUMERIC(20, 12),
    pricing                JSONB NOT NULL DEFAULT '{}'::jsonb,
    supported_parameters   JSONB NOT NULL DEFAULT '[]'::jsonb,
    supports_tools         BOOLEAN NOT NULL DEFAULT FALSE,
    expiration_date        DATE,
    alias_target           TEXT,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_model_catalogue_alias_target
    ON model_catalogue (alias_target)
    WHERE alias_target IS NOT NULL;

CREATE TABLE IF NOT EXISTS model_alias_events (
    id           BIGSERIAL PRIMARY KEY,
    alias        TEXT NOT NULL,
    old_target   TEXT,
    new_target   TEXT,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_alias_events_alias
    ON model_alias_events (alias, observed_at DESC);
