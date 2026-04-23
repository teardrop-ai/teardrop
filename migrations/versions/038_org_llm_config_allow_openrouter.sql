-- Relax org_llm_config.provider CHECK constraint to include 'openrouter'.
-- Teardrop uses OpenRouter as an OpenAI-compatible proxy to access models such as
-- DeepSeek V3.2 pinned to US-based inference (DeepInfra) while staying on a
-- single API key. Additive change — existing rows are unaffected.

ALTER TABLE org_llm_config
    DROP CONSTRAINT IF EXISTS org_llm_config_provider_check;

ALTER TABLE org_llm_config
    ADD CONSTRAINT org_llm_config_provider_check
        CHECK (provider IN ('anthropic', 'openai', 'google', 'openrouter'));
