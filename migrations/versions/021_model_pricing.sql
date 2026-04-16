-- Extend pricing_rules with provider/model columns for per-model pricing.

ALTER TABLE pricing_rules ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE pricing_rules ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_pricing_rules_provider_model
    ON pricing_rules (provider, model, effective_from DESC);
