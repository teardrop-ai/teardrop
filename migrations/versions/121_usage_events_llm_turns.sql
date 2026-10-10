-- Migration 121: per-turn LLM attribution on usage_events
-- Domain: billing / usage
-- Invariant: additive only. Each element records one LLM turn: configured
-- provider/model, token counts and, for OpenRouter, the gateway-reported
-- upstream_model, upstream_provider and upstream_cost_usd (USD, unmarked-up).
-- Existing rows default to an empty array; cost_usdc remains the billed total.

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS llm_turns JSONB NOT NULL DEFAULT '[]'::jsonb;
