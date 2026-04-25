-- Migration 041: BYOK orchestration pricing tier.
--
-- Adds is_byok column to pricing_rules so the billing engine can resolve a
-- separate rate for BYOK orgs (token-based orchestration fee) vs. standard
-- orgs (model-passthrough cost).
--
-- BYOK rates reflect orchestration overhead only — BYOK users pay their LLM
-- provider directly.  Initial rates: 50 atomic USDC per 1k tokens (~$0.00005/1k).
-- These are intentionally low and can be tuned via DB update without a deploy.
--
-- Resolution order (get_current_pricing_for_model with is_byok=True):
--   exact provider+model BYOK match → provider-level BYOK → global BYOK default

ALTER TABLE pricing_rules ADD COLUMN IF NOT EXISTS is_byok BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_pricing_rules_byok
    ON pricing_rules (is_byok, provider, model, effective_from DESC);

-- Global BYOK default (provider='', model='', is_byok=TRUE)
INSERT INTO pricing_rules
    (id, name, provider, model, is_byok, run_price_usdc,
     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)
VALUES
    ('byok-global-v1',
     'BYOK Orchestration (global default)',
     '', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-anthropic-v1',
     'BYOK Orchestration (Anthropic)',
     'anthropic', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-openai-v1',
     'BYOK Orchestration (OpenAI)',
     'openai', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-google-v1',
     'BYOK Orchestration (Google)',
     'google', '', TRUE,
     0, 50, 50, 0, NOW()),

    ('byok-openrouter-v1',
     'BYOK Orchestration (OpenRouter)',
     'openrouter', '', TRUE,
     0, 50, 50, 0, NOW())

ON CONFLICT (id) DO NOTHING;
