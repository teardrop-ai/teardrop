-- Migration 124: org-adjustable reasoning effort on org_llm_config
-- Domain: llm config
-- Invariant: additive. reasoning_effort is the org-wide level (NULL = platform
-- default per model); model_reasoning_effort maps 'provider:model' to a level
-- and takes precedence. Existing rows keep provider defaults.

ALTER TABLE org_llm_config
    ADD COLUMN IF NOT EXISTS reasoning_effort TEXT
        CHECK (reasoning_effort IN ('none', 'minimal', 'low', 'medium', 'high')),
    ADD COLUMN IF NOT EXISTS model_reasoning_effort JSONB NOT NULL DEFAULT '{}'::jsonb;
