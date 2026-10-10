-- Migration 122: retire Anthropic as a direct LLM provider
-- Domain: llm config
-- Invariant: data-only plus column default. Shared-key org configs on provider 'anthropic'
-- move to the platform default model via OpenRouter. BYOK rows are left
-- untouched (their key is Anthropic-only); they fail with a clear provider
-- error until the org re-uploads a supported provider config.

UPDATE org_llm_config
SET provider = 'openrouter',
    model = '~deepseek/deepseek-flash-latest',
    updated_at = NOW()
WHERE provider = 'anthropic'
  AND NOT is_byok;

ALTER TABLE org_llm_config ALTER COLUMN provider SET DEFAULT 'openrouter';
